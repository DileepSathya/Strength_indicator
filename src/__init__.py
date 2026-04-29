import logging
import os
import sys

log_dir="logs"
log_filepath=os.path.join(log_dir,"logging.log")
os.makedirs(log_dir,exist_ok=True)

logging.basicConfig(level=logging.INFO,
    format='[%(asctime)s]: %(name)s-%(levelname)s-%(message)s:',
    datefmt='%Y-%m-%d-%H:%M:%S',

   handlers=[
        logging.FileHandler(log_filepath),
        logging.StreamHandler(sys.stdout)
    ]
    )


logger=logging.getLogger("Compression_screener_logger")


