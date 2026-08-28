
from typing import Dict, Any
import json
from pobi_agent.logging import logger
from pobi_agent.constants import REUSABLE_CREDENTIALS_FILE

def load_reusable_credentials() -> Dict[str, Any]:
    """
    Load reusable credentials from the JSON file.
    
    Returns:
        Dict[str, Any]: Dictionary containing credentials data
    """
    try:

        with open(REUSABLE_CREDENTIALS_FILE, 'r', encoding='utf-8') as f:
            creds = f.read()
            return json.loads(creds)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        # 凭证钱包文件未配置是常态（多数任务不提供），静默降级避免每次
        # 发送请求都刷两条 warning 干扰日志/LLM 观察（原为 logger.warning）。
        logger.debug("Could not load reusable credentials: %s", e)
        return {"accounts": []}

def replace_credential_placeholders(request_data: str, account_index: int = 0) -> str:
    """
    Replace credential placeholders in request data with actual values 
        from reusable_credentials.json.
    
    Args:
        request_data (str): Raw HTTP request string containing placeholders
        account_index (int): Index of the account to use from the credentials file (default: 0)
        
    Returns:
        str: Request data with placeholders replaced by actual credential values
    """
    # 请求体不含占位符（dummy_*）时无需加载凭证钱包：避免为每个请求读一次
    # 不存在的文件并刷屏。只有真正携带占位符的请求才走替换流程。
    if "dummy_" not in request_data:
        return request_data
    credentials = load_reusable_credentials()
    accounts: Any = credentials.get("accounts", [])

    if not accounts or account_index >= len(accounts):
        # 有占位符但无可用账户：保持原样，debug 级提示（不刷屏）
        logger.debug("No account found at index %d", account_index)
        return request_data

    account = accounts[account_index]
    replaced_data = request_data
    # replacing the dummy email with the email
    replaced_data = replaced_data.replace(
        account.get("dummy_email"),
        # "<dummy_email>",
        account.get("email")
    )
    # replacing the dummy username with the username
    replaced_data = replaced_data.replace(
        account.get("dummy_username"),
        account.get("username")
    )
    # replacing the dummy password with the password
    replaced_data = replaced_data.replace(
        account.get("dummy_password"),
        account.get("password")
    )
    return replaced_data



