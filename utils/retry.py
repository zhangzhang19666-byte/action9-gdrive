#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
from typing import Callable, Any, List, Optional

def retry_with_account_switch(
    max_retries: int = 3,
    delay: int = 5,
    accounts: Optional[List[Dict]] = None,
    on_switch: Optional[Callable[[Dict], Any]] = None
):
    """
    通用重试装饰器，支持账号切换
    :param max_retries: 每个账号的最大重试次数
    :param delay: 重试间隔（秒）
    :param accounts: 账号列表 [{"username": "...", "password": "..."}]
    :param on_switch: 切换账号时执行的回调函数 (传入新账号字典)
    """
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            current_account_idx = 0
            retries = 0
            
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    print(f"⚠️ 执行 {func.__name__} 失败 (第 {retries} 次): {e}")
                    
                    if retries >= max_retries:
                        if accounts and current_account_idx < len(accounts) - 1:
                            current_account_idx += 1
                            retries = 0
                            new_account = accounts[current_account_idx]
                            print(f"🔄 达到最大重试次数，正在切换至账号 {new_account.get('username')}...")
                            if on_switch:
                                on_switch(new_account)
                            time.sleep(delay)
                            continue
                        else:
                            print(f"❌ 达到最大重试次数且无可用账号切换，操作终止。")
                            raise
                    
                    time.sleep(delay)
        return wrapper
    return decorator
