# -*- coding: utf-8 -*-
"""NCBI_genebank.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1-AOxH13r7LHZ6qtWLqm9GbO9DmQ0x8Wz
"""


import glob
import ftplib
import requests
from bs4 import BeautifulSoup
from itertools import islice
import pandas as pd
from Bio import SeqIO
from datasets import load_dataset
import time
import subprocess
import concurrent.futures
import logging


import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def batched(iterable, n):
    # batched('ABCDEFG', 3) --> ABC DEF G
    if n < 1:
        raise ValueError('n must be at least one')
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch

def calculate_total_size(url):
    response = requests.get(url)
    response.raise_for_status()  # Ensure we got a valid response
    soup = BeautifulSoup(response.content, 'html.parser')

    # Regular expression to find numbers followed by an 'M'
    pattern = re.compile(r'(\d+(\.\d+)?)M')
    matches = pattern.findall(str(soup))

    # Extract the first item from each tuple to get the numbers
    sizes = [match[0] for match in matches]
    sizes = [float(size) for size in sizes]
    print(sizes)
    return f'total size of files on the site is: {sum(sizes)}'

def get_links(url):
    # Fetch the content of the page
    response = requests.get(url)
    response.raise_for_status()  # Ensure we got a valid response

    # Parse the page using BeautifulSoup
    soup = BeautifulSoup(response.content, 'html.parser')

    # Find all anchor tags (links) and filter those ending with .gz
    gz_links = [link.get('href') for link in soup.find_all('a') if link.get('href', '').endswith('.gz')]
    return gz_links

url = "https://ftp.ncbi.nlm.nih.gov/genbank/"  # Replace this with the website you want to scrape
gz_links = get_links(url)
print(f"The website {url} contains {len(gz_links)} .gz links.")

def gunzip_file(file):
    """Function to decompress a single file and delete original."""
    logging.info(f"gunzipping {file}")
    subprocess.run(f"gunzip {file}", shell=True)
    logging.info(f"deleting {file}")
    subprocess.run(f"rm {file}", shell=True)

def process_file(file, file_count):
    logging.info(f"processing {file}")
    seqs_data = {
        'id': [],
        'sequence': [],
        'name': [],
        'description': [],
        'features': [],
        'seq_length': []
    }
    try:
        for record in SeqIO.parse(file, "genbank"):
            seqs_data['id'].append(str(record.id))
            seqs_data['sequence'].append(str(record.seq))
            seqs_data['name'].append(str(record.name))
            seqs_data['description'].append(str(record.description))
            seqs_data['features'].append(len(record.features))
            seqs_data['seq_length'].append(len(str(record.seq)))
            
        count = file_count[file]  
        df = pd.DataFrame(seqs_data)
        logging.info(f"writing {file}")
        df.to_parquet(f'file_{count}.parquet')
    except Exception as e:
        logging.error(f'Error processing {file}: {e}')
    finally:
        subprocess.run(f"rm {file}", shell=True)


def download_file(file):
    command = f'wget https://ftp.ncbi.nlm.nih.gov/genbank/{file}'
    subprocess.run(command, shell=True)


def process_total(files):
    try:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            executor.map(download_file, files)

        gz_files = glob.glob("**.gz")
        with concurrent.futures.ProcessPoolExecutor() as executor:
            executor.map(gunzip_file, gz_files)

        files = glob.glob("*.seq")
        file_count = {f: idx for idx, f in enumerate(files)}

        with concurrent.futures.ProcessPoolExecutor() as executor:
            results = list(executor.map(process_file, files, [file_count]*len(files)))

        for result in results:
            logging.info(result)
    except Exception as e:
        logging.error(f'Error in process_total: {e}')
    finally:
        print('loading parquet files')
        dataset = load_dataset("parquet", data_files="**.parquet")
        date = time.strftime("%Y%m%d")
        print('pushing to hub')

        dataset.push_to_hub(f'Hack90/ncbi_genbank_full_{date}')
        dataset.cleanup_cache_files()
        print('done')
        logging.info('Done with process_total')

process_total(gz_links)

