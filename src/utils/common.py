from src import logger
import yaml
import json
from datetime import datetime, timezone, timedelta
import os

def read_yaml(yaml_path):
 
    try:
        with open(yaml_path) as yaml_file:
            content=yaml.safe_load(yaml_file)
            
            return content
    except Exception as e:
        raise e


def write_yaml(path_to_yaml, content):
    try:
        with open(path_to_yaml, "w") as file:
            yaml.safe_dump(content, file, default_flow_style=False)
    except Exception as e:
        raise Exception(f"Error writing YAML file at {path_to_yaml}: {e}")
    
def format_symbol(symbol):
    return f"NSE:{symbol.upper()}-EQ"

def deformat_symbol(symbol):
    return symbol[4:-3]


from datetime import datetime, timezone, timedelta

# Define IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

def extract_candles_with_symbol(response_json, symbol):
    candles = response_json.get("candles", [])
    
    data_with_symbol = []
    for candle in candles:
        epoch_time = candle[0]
        dt = datetime.fromtimestamp(epoch_time, tz=IST)
        record = {
            "date": dt.strftime('%Y-%m-%d'),
            "time": dt.strftime('%H:%M:%S'),
            "open": candle[1],
            "high": candle[2],
            "low": candle[3],
            "close": candle[4],
            "volume": candle[5],
            "symbol": deformat_symbol(symbol=symbol)
        }
        data_with_symbol.append(record)
    
    return data_with_symbol



def save_to_raw_hist_data_json(new_data, json_path):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    # Load existing data
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            try:
                existing_data = json.load(f)
            except json.JSONDecodeError:
                existing_data = []
    else:
        existing_data = []

    # Merge and deduplicate by (date, symbol)
    all_data = existing_data + new_data
    with open(json_path, "w") as f:
        json.dump(list(all_data), f, indent=4)




def clear_hist_data_json(json_path):
    # Ensure the directory exists
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    # Overwrite the file with an empty list
    with open(json_path, "w") as f:
        json.dump([], f, indent=4)


def save_to_hist_data_json(new_data, json_path):
    # Ensure path exists
    os.makedirs(os.path.dirname(json_path), exist_ok=True)

    # Load existing data
    if os.path.exists(json_path):
        with open(json_path, "r") as f:
            try:
                existing_data = json.load(f)
            except json.JSONDecodeError:
                existing_data = []
    else:
        existing_data = []

    # Merge and deduplicate by (date, time, symbol)
    all_data = existing_data + new_data
    unique_data = {
        (item["date"], item["time"], item["symbol"]): item for item in all_data
    }.values()

    # Save to file
    with open(json_path, "w") as f:
        json.dump(list(unique_data), f, indent=4)

    print(f"✅ Data appended and saved to {json_path}")