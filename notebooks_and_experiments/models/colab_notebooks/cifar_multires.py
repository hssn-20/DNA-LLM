import math
import torch
import torch.nn as nn
import pywt
import logging
import os
import numpy as np
import torch
import torch.nn.functional as F
from torch.distributed import init_process_group
from torch.optim.lr_scheduler import LambdaLR
from torch.optim import Optimizer
import sys
import os
import argparse
from datetime import datetime
import logging
import torch
import torch.nn as nn
import torch.optim as optim
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import destroy_process_group
torch.backends.cuda.matmul.allow_tf32 = True
import torchvision
import torchvision.transforms as transforms
import transformers
import numpy as np

# from layers.multireslayer import MultiresLayer
from tqdm.auto import tqdm

class MultiresLayer(nn.Module):
    def __init__(self, d_model, kernel_size=None, depth=None, wavelet_init=None, tree_select="fading",
                 seq_len=None, dropout=0., memory_size=None, indep_res_init=False):
        super().__init__()

        self.kernel_size = kernel_size
        self.d_model = d_model
        self.seq_len = seq_len
        self.tree_select = tree_select
        if depth is not None:
            self.depth = depth
        elif seq_len is not None:
            self.depth = self.max_depth(seq_len)
        else:
            raise ValueError("Either depth or seq_len must be provided.")
        print("depth:", self.depth)

        if tree_select == "fading":
            self.m = self.depth + 1
        elif memory_size is not None:
            self.m = memory_size
        else:
            raise ValueError("memory_size must be provided when tree_select != 'fading'")

        with torch.no_grad():
            if wavelet_init is not None:
                self.wavelet = pywt.Wavelet(wavelet_init)
                h0 = torch.tensor(self.wavelet.dec_lo[::-1], dtype=torch.float32)
                h1 = torch.tensor(self.wavelet.dec_hi[::-1], dtype=torch.float32)
                self.h0 = nn.Parameter(torch.tile(h0[None, None, :], [d_model, 1, 1]))
                self.h1 = nn.Parameter(torch.tile(h1[None, None, :], [d_model, 1, 1]))
            elif kernel_size is not None:
                self.h0 = nn.Parameter(
                    torch.empty(d_model, 1, kernel_size).uniform_(-1., 1.) *
                    math.sqrt(2.0 / (kernel_size * 2))
                )
                self.h1 = nn.Parameter(
                    torch.empty(d_model, 1, kernel_size).uniform_(-1., 1.) *
                    math.sqrt(2.0 / (kernel_size * 2))
                )
            else:
                raise ValueError("kernel_size must be specified for non-wavelet initialization.")

            w_init = torch.empty(
                d_model, self.m + 1).uniform_(-1., 1.) * math.sqrt(2.0 / (2*self.m + 2))
            if indep_res_init:
                w_init[:, -1] = torch.empty(d_model).uniform_(-1., 1.)
            self.w = nn.Parameter(w_init)

        self.activation = nn.GELU()
        dropout_fn = nn.Dropout1d
        self.dropout = dropout_fn(dropout) if dropout > 0. else nn.Identity()

    def max_depth(self, L):
        depth = math.ceil(math.log2((L - 1) / (self.kernel_size - 1) + 1))
        return depth

    def forward(self, x):
        if self.tree_select == "fading":
            y = forward_fading(x, self.h0, self.h1, self.w, self.depth, self.kernel_size)
        elif self.tree_select == "uniform":
            y = forward_uniform(x, self.h0, self.h1, self.w, self.depth, self.kernel_size, self.m)
        else:
            raise NotImplementedError()
        y = self.dropout(self.activation(y))
        return y


def forward_fading(x, h0, h1, w, depth, kernel_size):
    res_lo = x
    y = 0.
    dilation = 1
    for i in range(depth, 0, -1):
        padding = dilation * (kernel_size - 1)
        res_lo_pad = torch.nn.functional.pad(res_lo, (padding, 0), "constant", 0)
        res_hi = torch.nn.functional.conv1d(res_lo_pad, h1, dilation=dilation, groups=x.shape[1])
        res_lo = torch.nn.functional.conv1d(res_lo_pad, h0, dilation=dilation, groups=x.shape[1])
        y += w[:, i:i + 1] * res_hi
        dilation *= 2

    y += w[:, :1] * res_lo
    y += x * w[:, -1:]
    return y


def forward_uniform(x, h0, h1, w, depth, kernel_size, memory_size):
    # x: [bs, d_model, L]
    coeff_lst = []
    dilation_lst = [1]
    dilation = 1
    res_lo = x
    for _ in range(depth):
        padding = dilation * (kernel_size - 1)
        res_lo_pad = torch.nn.functional.pad(res_lo, (padding, 0), "constant", 0)
        res_hi = torch.nn.functional.conv1d(res_lo_pad, h1, dilation=dilation, groups=x.shape[1])
        res_lo = torch.nn.functional.conv1d(res_lo_pad, h0, dilation=dilation, groups=x.shape[1])
        coeff_lst.append(res_hi)
        dilation *= 2
        dilation_lst.append(dilation)
    coeff_lst.append(res_lo)
    coeff_lst = coeff_lst[::-1]
    dilation_lst = dilation_lst[::-1]

    # y: [bs, d_model, L]
    y = uniform_tree_select(coeff_lst, dilation_lst, w, kernel_size, memory_size)
    y = y + x * w[:, -1:]
    return y


def uniform_tree_select(coeff_lst, dilation_lst, w, kernel_size, memory_size):
    latent_dim = 1
    y_lst = [coeff_lst[0] * w[:, 0, None]]
    layer_dim = 1
    dilation_lst[0] = 1
    for l, coeff_l in enumerate(coeff_lst[1:]):
        if latent_dim + layer_dim > memory_size:
            layer_dim = memory_size - latent_dim
        # layer_w: [d, layer_dim]
        layer_w = w[:, latent_dim:latent_dim + layer_dim]
        # coeff_l_pad: [bs, d, L + left_pad]
        left_pad = (layer_dim - 1) * dilation_lst[l]
        coeff_l_pad = torch.nn.functional.pad(coeff_l, (left_pad, 0), "constant", 0)
        # y: [bs, d, L]
        y = torch.nn.functional.conv1d(
            coeff_l_pad,
            torch.flip(layer_w[:, None, :], (-1,)),
            dilation=dilation_lst[l],
            groups=coeff_l.shape[1],
        )
        y_lst.append(y)
        latent_dim += layer_dim
        if latent_dim >= memory_size:
            break
        layer_dim = 2 * (layer_dim - 1) + kernel_size
    return sum(y_lst)


def apply_norm(x, norm, batch_norm=False):
    if batch_norm:
        return norm(x)
    else:
        return norm(x.transpose(-1, -2)).transpose(-1, -2)


def ddp_setup(rank, world_size, port):
    """
    Args:
        rank: Unique identifier of each process
        world_size: Total number of processes
    """
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = port
    init_process_group(backend="nccl", rank=rank, world_size=world_size)


def split_train_val(train, val_split):
    train_len = int(len(train) * (1.0-val_split))
    train, val = torch.utils.data.random_split(
        train,
        (train_len, len(train) - train_len),
        generator=torch.Generator().manual_seed(42),
    )
    return train, val


class DistributedSamplerNoDuplicate(torch.utils.data.DistributedSampler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.drop_last and len(self.dataset) % self.num_replicas != 0:
            # some ranks may have less samples, that's fine
            if self.rank >= len(self.dataset) % self.num_replicas:
                self.num_samples -= 1
            self.total_size = len(self.dataset)


def log_sum_exp(x):
    """ numerically stable log_sum_exp implementation that prevents overflow """
    # TF ordering
    axis  = len(x.size()) - 1
    m, _  = torch.max(x, dim=axis)
    m2, _ = torch.max(x, dim=axis, keepdim=True)
    return m + torch.log(torch.sum(torch.exp(x - m2), dim=axis))


def log_prob_from_logits(x):
    """ numerically stable log_softmax implementation that prevents overflow """
    # TF ordering
    axis = len(x.size()) - 1
    m, _ = torch.max(x, dim=axis, keepdim=True)
    return x - m - torch.log(torch.sum(torch.exp(x - m), dim=axis, keepdim=True))


# https://github.com/pclucas14/pixel-cnn-pp/blob/master/utils.py
def discretized_mix_logistic_loss(x, l):
    """ log-likelihood for mixture of discretized logistics, assumes the data has been rescaled to [-1,1] interval """
    # Pytorch ordering
    x = x.permute(0, 2, 3, 1)
    l = l.permute(0, 2, 3, 1)
    xs = [int(y) for y in x.size()]
    ls = [int(y) for y in l.size()]

    # here and below: unpacking the params of the mixture of logistics
    nr_mix = int(ls[-1] / 10)
    logit_probs = l[:, :, :, :nr_mix]
    l = l[:, :, :, nr_mix:].contiguous().view(xs + [nr_mix * 3]) # 3 for mean, scale, coef
    means = l[:, :, :, :, :nr_mix]
    # log_scales = torch.max(l[:, :, :, :, nr_mix:2 * nr_mix], -7.)
    log_scales = torch.clamp(l[:, :, :, :, nr_mix:2 * nr_mix], min=-7.)

    coeffs = torch.tanh(l[:, :, :, :, 2 * nr_mix:3 * nr_mix])
    # here and below: getting the means and adjusting them based on preceding
    # sub-pixels
    x = x.contiguous()
    x = x.unsqueeze(-1) + torch.zeros(xs + [nr_mix], device=x.device)
    m2 = (means[:, :, :, 1, :] + coeffs[:, :, :, 0, :]
                * x[:, :, :, 0, :]).view(xs[0], xs[1], xs[2], 1, nr_mix)

    m3 = (means[:, :, :, 2, :] + coeffs[:, :, :, 1, :] * x[:, :, :, 0, :] +
                coeffs[:, :, :, 2, :] * x[:, :, :, 1, :]).view(xs[0], xs[1], xs[2], 1, nr_mix)

    means = torch.cat((means[:, :, :, 0, :].unsqueeze(3), m2, m3), dim=3)
    centered_x = x - means
    inv_stdv = torch.exp(-log_scales)
    plus_in = inv_stdv * (centered_x + 1. / 255.)
    cdf_plus = torch.sigmoid(plus_in)
    min_in = inv_stdv * (centered_x - 1. / 255.)
    cdf_min = torch.sigmoid(min_in)
    # log probability for edge case of 0 (before scaling)
    log_cdf_plus = plus_in - F.softplus(plus_in)
    # log probability for edge case of 255 (before scaling)
    log_one_minus_cdf_min = -F.softplus(min_in)
    cdf_delta = cdf_plus - cdf_min  # probability for all other cases
    mid_in = inv_stdv * centered_x
    # log probability in the center of the bin, to be used in extreme cases
    # (not actually used in our code)
    log_pdf_mid = mid_in - log_scales - 2. * F.softplus(mid_in)

    # now select the right output: left edge case, right edge case, normal
    # case, extremely low prob case (doesn't actually happen for us)

    # this is what we are really doing, but using the robust version below for extreme cases in other applications and to avoid NaN issue with tf.select()
    # log_probs = tf.select(x < -0.999, log_cdf_plus, tf.select(x > 0.999, log_one_minus_cdf_min, tf.log(cdf_delta)))

    # robust version, that still works if probabilities are below 1e-5 (which never happens in our code)
    # tensorflow backpropagates through tf.select() by multiplying with zero instead of selecting: this requires use to use some ugly tricks to avoid potential NaNs
    # the 1e-12 in tf.maximum(cdf_delta, 1e-12) is never actually used as output, it's purely there to get around the tf.select() gradient issue
    # if the probability on a sub-pixel is below 1e-5, we use an approximation
    # based on the assumption that the log-density is constant in the bin of
    # the observed sub-pixel value

    inner_inner_cond = (cdf_delta > 1e-5).float()
    inner_inner_out  = inner_inner_cond * torch.log(torch.clamp(cdf_delta, min=1e-12)) + (1. - inner_inner_cond) * (log_pdf_mid - np.log(127.5))
    inner_cond       = (x > 0.999).float()
    inner_out        = inner_cond * log_one_minus_cdf_min + (1. - inner_cond) * inner_inner_out
    cond             = (x < -0.999).float()
    log_probs        = cond * log_cdf_plus + (1. - cond) * inner_out
    log_probs        = torch.sum(log_probs, dim=3) + log_prob_from_logits(logit_probs)

    return -torch.sum(log_sum_exp(log_probs))


def sample_from_discretized_mix_logistic(l, nr_mix):
    # Pytorch ordering
    l = l.permute(0, 2, 3, 1)
    ls = [int(y) for y in l.size()]
    xs = ls[:-1] + [3]

    # unpack parameters
    logit_probs = l[:, :, :, :nr_mix]
    l = l[:, :, :, nr_mix:].contiguous().view(xs + [nr_mix * 3])
    # sample mixture indicator from softmax
    temp = torch.empty(logit_probs.size(), device=l.device)
    temp.uniform_(1e-5, 1. - 1e-5)
    temp = logit_probs.data - torch.log(- torch.log(temp))
    _, argmax = temp.max(dim=3)

    one_hot = to_one_hot(argmax, nr_mix)
    sel = one_hot.view(xs[:-1] + [1, nr_mix])
    # select logistic parameters
    means = torch.sum(l[:, :, :, :, :nr_mix] * sel, dim=4)
    log_scales = torch.clamp(torch.sum(
        l[:, :, :, :, nr_mix:2 * nr_mix] * sel, dim=4), min=-7.)
    coeffs = torch.sum(torch.tanh(
        l[:, :, :, :, 2 * nr_mix:3 * nr_mix]) * sel, dim=4)
    # sample from logistic & clip to interval
    # we don't actually round to the nearest 8bit value when sampling
    u = torch.empty(means.size(), device=means.device)
    u.uniform_(1e-5, 1. - 1e-5)
    x = means + torch.exp(log_scales) * (torch.log(u) - torch.log(1. - u))
    x0 = torch.clamp(torch.clamp(x[:, :, :, 0], min=-1.), max=1.)
    x1 = torch.clamp(torch.clamp(
       x[:, :, :, 1] + coeffs[:, :, :, 0] * x0, min=-1.), max=1.)
    x2 = torch.clamp(torch.clamp(
       x[:, :, :, 2] + coeffs[:, :, :, 1] * x0 + coeffs[:, :, :, 2] * x1, min=-1.), max=1.)

    out = torch.cat([x0.view(xs[:-1] + [1]), x1.view(xs[:-1] + [1]), x2.view(xs[:-1] + [1])], dim=3)
    # put back in Pytorch ordering
    out = out.permute(0, 3, 1, 2)
    return out


def to_one_hot(tensor, n, fill_with=1.):
    # we perform one hot encore with respect to the last axis
    one_hot = torch.FloatTensor(tensor.size() + (n,), device=tensor.device).zero_()
    one_hot.scatter_(len(tensor.size()), tensor.unsqueeze(-1), fill_with)
    return one_hot


def sample_from_discretized_mix_logistic_1d(l, nr_mix):
    # Pytorch ordering
    l = l.permute(0, 2, 3, 1)
    ls = [int(y) for y in l.size()]
    xs = ls[:-1] + [1] #[3]

    # unpack parameters
    logit_probs = l[:, :, :, :nr_mix]
    l = l[:, :, :, nr_mix:].contiguous().view(xs + [nr_mix * 2]) # for mean, scale

    # sample mixture indicator from softmax
    temp = torch.FloatTensor(logit_probs.size(), device=l.device)
    temp.uniform_(1e-5, 1. - 1e-5)
    temp = logit_probs.data - torch.log(- torch.log(temp))
    _, argmax = temp.max(dim=3)

    one_hot = to_one_hot(argmax, nr_mix)
    sel = one_hot.view(xs[:-1] + [1, nr_mix])
    # select logistic parameters
    means = torch.sum(l[:, :, :, :, :nr_mix] * sel, dim=4)
    log_scales = torch.clamp(torch.sum(
        l[:, :, :, :, nr_mix:2 * nr_mix] * sel, dim=4), min=-7.)
    u = torch.FloatTensor(means.size(), device=l.device)
    u.uniform_(1e-5, 1. - 1e-5)
    x = means + torch.exp(log_scales) * (torch.log(u) - torch.log(1. - u))
    x0 = torch.clamp(torch.clamp(x[:, :, :, 0], min=-1.), max=1.)
    out = x0.unsqueeze(1)
    return out


def setup_logger(name, src, result_path, filename="log"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    log_path = os.path.join(result_path, filename)
    makedirs(log_path)
    info_file_handler = logging.FileHandler(log_path)
    info_file_handler.setLevel(logging.INFO)
    logger.addHandler(info_file_handler)
    logger.info(src)
    with open(src) as f:
        logger.info(f.read())
    return logger


def makedirs(filename):
    if not os.path.exists(os.path.dirname(filename)):
        os.makedirs(os.path.dirname(filename))


def ensure_path(path, other):
    if os.path.exists(path):
        return path
    return other


def count_parameters(model):
    # for p in model.parameters():
    #     if p.requires_grad:
    #         print(p.shape)
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


class DotDict(dict):
    """dot.notation access to dictionary attributes
    From https://stackoverflow.com/questions/2352181/how-to-use-a-dot-to-access-members-of-dictionary
    Note that there are issues with updating values of nested DotDicts
    """

    def __getattr__(*args):
        # Allow nested dicts
        val = dict.get(*args)
        return DotDict(val) if type(val) is dict else val

    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__
    __dir__ = dict.keys


class DummyWandb:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.config = {}
        self.name = ""
        self.id = ""
        self.path = ""
        self.dir = "./"

    @staticmethod
    def init(*args, **kwargs):
        return DummyWandb(*args, **kwargs)

    def log(self, *args, **kwargs):
        return

    def watch(self, *args, **kwargs):
        return

    def finish(self, *args, **kwargs):
        return

    def save(self, *args, **kwargs):
        return

def get_cosine_schedule_with_warmup(
    optimizer: Optimizer, num_warmup_steps: int, num_training_steps: int, num_cycles: float = 0.5, last_epoch: int = -1
):
    """ From: https://github.com/huggingface/transformers/blob/main/src/transformers/optimization.py,
    This way we don't have dependency on the `transformers` package.

    Create a schedule with a learning rate that decreases following the values of the cosine function between the
    initial lr set in the optimizer to 0, after a warmup period during which it increases linearly between 0 and the
    initial lr set in the optimizer.

    Args:
        optimizer ([`~torch.optim.Optimizer`]):
            The optimizer for which to schedule the learning rate.
        num_warmup_steps (`int`):
            The number of steps for the warmup phase.
        num_training_steps (`int`):
            The total number of training steps.
        num_cycles (`float`, *optional*, defaults to 0.5):
            The number of waves in the cosine schedule (the defaults is to just decrease from the max value to 0
            following a half-cosine).
        last_epoch (`int`, *optional*, defaults to -1):
            The index of the last epoch when resuming training.

    Return:
        `torch.optim.lr_scheduler.LambdaLR` with the appropriate schedule.
    """

    def lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        progress = float(current_step - num_warmup_steps) / float(max(1, num_training_steps - num_warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * float(num_cycles) * 2.0 * progress)))

    return LambdaLR(optimizer, lr_lambda, last_epoch)


class MultiresAR(nn.Module):

    def __init__(
        self,
        d_input,
        nr_logistic_mix,
        d_model=256,
        n_layers=4,
        dropout=0.2,
        batchnorm=False,
        encoder="linear",
        n_tokens=None,
        layer_type="multires",
        max_length=None,
        hinit=None,
        depth=None,
        tree_select="fading",
        d_mem=None,
        kernel_size=2,
        indep_res_init=False,
    ):
        super().__init__()

        self.batchnorm = batchnorm
        self.max_length = max_length
        self.depth = depth
        self.nr_logistic_mix = nr_logistic_mix
        if encoder == "linear":
            self.encoder = nn.Conv1d(d_input, d_model, 1)
        elif encoder == "embedding":
            self.encoder = nn.Embedding(n_tokens, d_model)
        self.activation = nn.GELU()

        # Stack sequence modeling layers as residual blocks
        self.seq_layers = nn.ModuleList()
        self.mixing_layers = nn.ModuleList()
        self.norms = nn.ModuleList()

        if batchnorm:
            norm_func = nn.BatchNorm1d
        else:
            norm_func = nn.LayerNorm

        for _ in range(n_layers):
            if layer_type == "multires":
                layer = MultiresLayer(
                    d_model,
                    kernel_size=kernel_size,
                    depth=depth,
                    wavelet_init=hinit,
                    tree_select=tree_select,
                    seq_len=max_length,
                    dropout=dropout,
                    memory_size=d_mem,
                    indep_res_init=indep_res_init,
                )
            else:
                raise NotImplementedError()
            self.seq_layers.append(layer)

            activation_scaling = 2
            mixing_layer = nn.Sequential(
                nn.Conv1d(d_model, activation_scaling * d_model, 1),
                nn.GLU(dim=-2),
                nn.Dropout1d(dropout),
                nn.Conv1d(d_model, d_model, 1),
            )

            self.mixing_layers.append(mixing_layer)
            self.norms.append(norm_func(d_model))

        # Linear layer maps to mixiture of Logistics parameters
        num_mix = 3 if d_input == 1 else 10
        self.d_output = num_mix * nr_logistic_mix
        self.decoder = nn.Conv1d(d_model, self.d_output, 1)

    def forward(self, x, **kwargs):
        """Input shape: [bs, d_input, seq_len]. """
        # conv: [bs, d_input, seq_len] -> [bs, d_model, seq_len]
        # embedding: [bs, seq_len] -> [bs, seq_len, d_model]
        # Shift input by 1 pixel to perform autoregressive modeling
        x = torch.nn.functional.pad(x[..., :-1], (1, 0), "constant", 0)
        x = self.encoder(x)
        if isinstance(self.encoder, nn.Embedding):
            x = x.transpose(-1, -2)

        for layer, mixing_layer, norm in zip(
                self.seq_layers, self.mixing_layers, self.norms):
            x_orig = x
            x = layer(x)
            x = mixing_layer(x)
            x = x + x_orig

            x = apply_norm(x, norm, self.batchnorm)

        # out: [bs, d_output, seq_len]
        out = self.decoder(x)
        return out


def setup_optimizer(model, lr, epochs, iters_per_epoch, warmup):
    params = model.parameters()
    optimizer = optim.Adam(params, lr=lr)
    # We use the cosine annealing schedule sometimes with a linear warmup
    total_steps = epochs * iters_per_epoch
    if warmup > 0:
        warmup_steps = warmup * iters_per_epoch
        scheduler = transformers.get_cosine_schedule_with_warmup(optimizer, warmup_steps, total_steps)
    else:
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, total_steps)
    return optimizer, scheduler


def train(device, epoch, trainloader, model, optimizer, scheduler, criterion, data_shape):
    data_shape = list(data_shape)
    model.train()
    train_loss = 0
    total = 0
    trainloader.sampler.set_epoch(epoch)
    pbar = enumerate(trainloader)
    if device == 0:
        pbar = tqdm(pbar)
    for batch_idx, batch in pbar:
        inputs, _ = batch
        inputs = inputs.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        outputs = model(inputs)
        loss = criterion(inputs.reshape([-1] + data_shape), outputs.reshape([-1, outputs.shape[1]] + data_shape[-2:]))
        loss.backward()
        optimizer.step()
        scheduler.step()

        train_loss += loss.item()
        total += inputs.shape[0]
        if device == 0:
            bpd_factor = total * np.prod(data_shape) * np.log(2.)
            train_bpd = train_loss / bpd_factor
            pbar.set_description(
                'Epoch {} | Batch Idx: ({}/{}) | Loss: {:.4f} | LR: {:.5f}'
                .format(epoch, batch_idx, len(trainloader), train_bpd, scheduler.get_lr()[0])
            )
    return train_loss / (total * np.prod(data_shape) * np.log(2.))


def eval(device, dataloader, model, criterion, data_shape):
    data_shape = list(data_shape)
    model.eval()
    eval_loss = 0
    total = 0
    with torch.no_grad():
        if device == 0:
            pbar = tqdm(enumerate(dataloader))
        else:
            pbar = enumerate(dataloader)
        for batch_idx, batch in pbar:
            inputs, _ = batch
            inputs = inputs.to(device)
            outputs = model(inputs)
            loss = criterion(inputs.reshape([-1] + data_shape), outputs.reshape([-1, outputs.shape[1]] + data_shape[-2:]))

            eval_loss += loss.item()
            total += inputs.size(0)

            if device == 0:
                bpd_factor = total * np.prod(data_shape) * np.log(2.)
                eval_bpd = eval_loss / bpd_factor
                pbar.set_description(
                    'Batch Idx: ({}/{}) | Loss: {:.4f}'
                    .format(batch_idx, len(dataloader), eval_bpd)
                )

    # return eval_loss / (total * np.prod(data_shape) * np.log(2.))
    return eval_loss, total


def rescaling(x):
    return (x - 0.5) * 2.

def image2seq(x):
    return x.view(3, 1024)


def main(rank, world_size, args):
    ddp_setup(rank, world_size, args.port)
    assert args.batch_size % world_size == 0
    per_device_batch_size = args.batch_size // world_size
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    if args.dataset == 'cifar10':
        transform = transforms.Compose([transforms.ToTensor(), rescaling, image2seq])

        trainset = torchvision.datasets.CIFAR10(
            root='./data/cifar/', train=True, download=True, transform=transform)
        trainset, valset = split_train_val(trainset, val_split=0.04)

        testset = torchvision.datasets.CIFAR10(
            root='./data/cifar/', train=False, download=True, transform=transform)

        d_input = 3
        data_shape = (3, 32, 32)

        testloader = torch.utils.data.DataLoader(
            testset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers,
            pin_memory=True, sampler=DistributedSamplerNoDuplicate(testset, shuffle=False))

        valloader = torch.utils.data.DataLoader(
            valset, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers,
            pin_memory=True, sampler=DistributedSamplerNoDuplicate(valset, shuffle=False))

        trainloader = torch.utils.data.DataLoader(
            trainset, batch_size=per_device_batch_size, num_workers=args.num_workers, shuffle=False,
            pin_memory=True, drop_last=True, sampler=DistributedSampler(trainset))
        encoder = "linear"
        n_tokens = None
        max_length = 1024

    else:
        raise NotImplementedError()

    torch.cuda.set_device(rank)
    torch.backends.cudnn.benchmark = True

    # Model
    model = MultiresAR(
        d_input=d_input,
        nr_logistic_mix=10,
        d_model=args.d_model,
        n_layers=args.n_layers,
        dropout=args.dropout,
        batchnorm=args.batchnorm,
        encoder=encoder,
        n_tokens=n_tokens,
        layer_type=args.layer,
        max_length=max_length,
        hinit=args.hinit,
        depth=args.depth,
        tree_select=args.tree_select,
        d_mem=args.d_mem,
        kernel_size=args.kernel_size,
        indep_res_init=args.indep_res_init,
    ).to(rank)
    if args.batchnorm:
        model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = DDP(model, device_ids=[rank])

    criterion = discretized_mix_logistic_loss
    optimizer, scheduler = setup_optimizer(
        model, lr=args.lr, epochs=args.epochs, iters_per_epoch=len(trainloader), warmup=args.warmup)

    if args.resume is not None:
        log_dir = 'logs/ar/{}/{}'.format(args.dataset, args.resume)
        print('==> Resuming from checkpoint..')
        # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
        checkpoint = torch.load('{}/ckpt.pth'.format(log_dir), map_location=map_location)
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        scheduler.load_state_dict(checkpoint['scheduler'])
        start_epoch = checkpoint['epoch']
        best_val_loss = checkpoint['val_loss']
        final_test_loss = checkpoint['test_loss']
    else:
        log_dir = 'logs/ar/{}/{}'.format(args.dataset, datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(log_dir, exist_ok=True)
        start_epoch = 0
        best_val_loss = np.inf
        final_test_loss = 0

    if rank == 0:
        logger = logging.getLogger(args.dataset)
        logger.setLevel(logging.INFO)
        log_path = os.path.join(log_dir, "log.txt")
        info_file_handler = logging.FileHandler(log_path)
        console_handler = logging.StreamHandler()
        logger.addHandler(info_file_handler)
        logger.addHandler(console_handler)
        logger.info("Total number of parameters: {}".format(count_parameters(model)))

    for epoch in range(start_epoch, args.epochs):
        train_loss = train(rank, epoch, trainloader, model, optimizer, scheduler, criterion, data_shape)
        if (epoch + 1) % args.test_every == 0:
            val_loss_sum, val_total = eval(rank, valloader, model, criterion, data_shape)
            val_metrics = torch.tensor([val_loss_sum, val_total], dtype=torch.float64).to(rank)
            dist.all_reduce(val_metrics, dist.ReduceOp.SUM, async_op=False)
            val_loss = val_metrics[0].item() / (val_metrics[1].item() * np.prod(data_shape) * np.log(2.))

            test_loss_sum, total = eval(rank, testloader, model, criterion, data_shape)
            metrics = torch.tensor([test_loss_sum, total], dtype=torch.float64).to(rank)
            dist.all_reduce(metrics, dist.ReduceOp.SUM, async_op=False)
            test_loss = metrics[0].item() / (metrics[1].item() * np.prod(data_shape) * np.log(2.))

            if val_loss <= best_val_loss:
                best_val_loss = val_loss
                final_test_loss = test_loss
                if rank == 0:
                    state = {
                        'model': model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                        'scheduler': scheduler.state_dict(),
                        'train_loss': train_loss,
                        'test_loss': test_loss,
                        'epoch': epoch,
                        'val_loss': val_loss,
                    }
                    torch.save(state, os.path.join(log_dir, "ckpt.pth"))

        if rank == 0:
            if (epoch + 1) % args.test_every == 0:
                logger.info("{}, {}, {}, {}".format(epoch, train_loss, val_loss, test_loss))
            else:
                logger.info("{}, {}, {}, {}".format(epoch, train_loss, -1, -1))
            with open(os.path.join(log_dir, 'args.txt'), 'w') as f:
                f.write('\n'.join(sys.argv[1:]))
    if rank == 0:
        logger.info("FINAL: test bits per dim={}".format(final_test_loss))
    destroy_process_group()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Training
    parser.add_argument('--lr', default=0.003, type=float, help='Learning rate')
    parser.add_argument('--epochs', default=250, type=int, help='Training epochs')
    parser.add_argument('--warmup', default=0, type=int, help='Number of warmup epochs')
    # Data
    parser.add_argument('--dataset', default='cifar10', choices=['cifar10'], type=str)
    parser.add_argument('--num_workers', default=4, type=int,
                        help='Number of workers for dataloader')
    parser.add_argument('--batch_size', default=64, type=int, help='Total batch size')
    # Model
    parser.add_argument('--n_layers', default=4, type=int, help='Number of layers')
    parser.add_argument('--d_model', default=128, type=int, help='Model dimension')
    parser.add_argument('--layer', default='multires', choices=['multires'], type=str,
                        help='Sequence modeling layer type')
    parser.add_argument('--d_mem', default=None, type=int,
                        help='memory size, must be None for tree_select=fading')
    parser.add_argument('--dropout', default=0.1, type=float, help='Dropout rate')
    parser.add_argument('--batchnorm', action='store_true',
                        help='Replace layernorm with batchnorm')
    parser.add_argument('--hinit', default=None, type=str, help='Wavelet init')
    parser.add_argument('--depth', default=None, type=int, help='depth of each layer')
    parser.add_argument('--tree_select', default="fading", choices=["uniform", "fading"],
                        help="Which part of the tree as memory")
    parser.add_argument('--kernel_size', default=2, type=int,
                        help='Filter size, only used when hinit=None')
    parser.add_argument('--indep_res_init', action='store_true',
                        help="Initialize w2 indepdent of z size in <w, [z, x]> = w1 z + w2 x")
    # Others
    parser.add_argument('--test_every', default=1, type=int, help='Every x epochs to eval the model')
    parser.add_argument('--resume', default=None, type=str,
                        help='The log directory to resume from checkpoint')
    parser.add_argument('--port', default='12669', type=str, help='data parallel port')
    parser.add_argument('--seed', default=1, type=int, help='Random seed')

    args = parser.parse_args()

    world_size = torch.cuda.device_count()
    mp.spawn(main, args=(world_size, args), nprocs=world_size)