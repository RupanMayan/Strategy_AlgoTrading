import requests

def send_strategy_signal(host_url, webhook_id, symbol, action, position_size=None):
    """
    Send a strategy signal via webhook
    
    Args:
        host_url (str): Base URL of the OpenAlgo server
        webhook_id (str): Strategy's webhook ID
        symbol (str): Trading symbol
        action (str): "BUY" or "SELL"
        position_size (int, optional): Required for BOTH mode
    """
    # Construct webhook URL
    webhook_url = f"{host_url}/strategy/webhook/{webhook_id}"
    
    # Prepare message
    post_message = {
        "symbol": symbol,
        "action": action.upper()
    }
    
    # Add position_size for BOTH mode
    if position_size is not None:
        post_message["position_size"] = str(position_size)
    
    try:
        response = requests.post(webhook_url, json=post_message)
        if response.status_code == 200:
            print(f"Signal sent successfully: {post_message}")
        else:
            print(f"Error sending signal. Status: {response.status_code}")
            print(f"Response: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")

# Example usage
host = "http://127.0.0.1:5000"
webhook_id = "9a8a3b13-7826-4894-9586-454a97908b83"

# Long entry example (BOTH mode)
send_strategy_signal(host, webhook_id, "SBIN", "BUY", 1)

# Short entry example (BOTH mode)
send_strategy_signal(host, webhook_id, "SBIN", "SELL", 0)
