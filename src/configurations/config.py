from src import logger
from src.utils.common import read_yaml

from src.constants import *

class config_manager:
    def login_info():
        try:
            yaml_file=read_yaml(CONFIG_FILE_PATH)
            redirect_urL=yaml_file["Input_params"]["redirect_uri"]
            client_id=yaml_file["Input_params"]["client_id"]
            secret_key=yaml_file["Input_params"]["secret_key"]
            grant_type=yaml_file["Input_params"]["grant_type"]
            response_type=yaml_file["Input_params"]["response_type"]
            state=yaml_file["Input_params"]["state"]

            logger.info("Successfully retrieved login configuration.")

            return redirect_urL, client_id, secret_key, grant_type, response_type, state

        except Exception as e:
            logger.error(f"Error while reading login config: {e}")
            raise
    def authentication():
        yaml_file=read_yaml(CONFIG_FILE_PATH)
        auth_code=yaml_file["Input_params"]["auth_code"]
        client_id=yaml_file["Input_params"]["client_id"]

        logger.info("Successfully retrieved authentication code.")

        return client_id,auth_code
    
    def json_file_path():
        yaml_file=read_yaml(CONFIG_FILE_PATH)
        json_path=yaml_file["data_saving"]["json_file_path"]


        return json_path
    def data_loading_config():
        yaml_file=read_yaml(CONFIG_FILE_PATH)
        symbol_list=yaml_file['data_loading_configs']['symbol_list']

        logger.info("symbol list retrieved successful")

        return symbol_list
    
    def csv_file_path():
        yaml_file=read_yaml(CONFIG_FILE_PATH)
        csv_path=yaml_file["data_saving"]["csv_path"]


        return csv_path
