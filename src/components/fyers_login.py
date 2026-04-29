from src import logger
from fyers_apiv3 import fyersModel
import webbrowser
from src.utils.common import read_yaml, write_yaml
from src.constants import CONFIG_FILE_PATH
from src.configurations.config import config_manager 
import datetime


class fyers_login:
    @staticmethod
    def login():
        config_data = read_yaml(CONFIG_FILE_PATH)

        # Check if access token exists and is recent
        auth_code = config_data.get("Input_params", {}).get("auth_code")
        last_login = config_data.get("Input_params", {}).get("last_login_time")

        if auth_code and last_login:
            last_login_time = datetime.datetime.strptime(last_login, "%Y-%m-%d %H:%M:%S")
            now = datetime.datetime.now()
            if (now - last_login_time) < datetime.timedelta(hours=24):
                logger.info("Previous login is still valid. Skipping login step by default.")
                user_input = input("Previous login is still valid. Do you want to login again? (y/n): ").strip().lower()
                if user_input != 'y':
                    logger.info("Login skipped by user choice.")
                    return

        # Proceed with login
        redirect_uri, client_id, secret_key, grant_type, response_type, state = config_manager.login_info()

        appSession = fyersModel.SessionModel(
            client_id=client_id,
            redirect_uri=redirect_uri,
            response_type=response_type,
            state=state,
            secret_key=secret_key,
            grant_type=grant_type
        )

        generateTokenUrl = appSession.generate_authcode()
        webbrowser.open(generateTokenUrl, new=1)

        auth_code = input("Enter the authentication code: ")
        appSession.set_token(auth_code)
        response = appSession.generate_token()

        try:
            access_token = response["access_token"]
            config_data["Input_params"]["auth_code"] = access_token
            config_data["Input_params"]["last_login_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            write_yaml(CONFIG_FILE_PATH, config_data)
            logger.info("Access token and timestamp saved successfully in config.yaml")

        except Exception as e:
            print("Error:", e)
            print("Response:", response)

