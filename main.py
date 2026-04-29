import webbrowser
from src.components.fyers_live_data import run_live_market_stream
from src.components import stream_server
from src import logger
from src.components.fyers_login import fyers_login


def login():
    STAGE = "login Stage"
    try:
        logger.info(f"-----{STAGE}-----")
        fyers_login.login()
        logger.info(f"{STAGE} sucessful")
    except Exception as e:
        logger.exception(e)
        raise e


if __name__ == "__main__":
    login()
    stream_server.start()
    webbrowser.open("http://localhost:5050")
    run_live_market_stream()