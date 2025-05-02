#!/usr/bin/env python
"""
Backpack Exchange 網格交易程序主執行文件
"""
import argparse
import sys
import os

from logger import setup_logger
from config import API_KEY, SECRET_KEY, WS_PROXY
from cli.commands import main_cli
from strategies.grid_trader import GridTrader

logger = setup_logger("main")

def parse_arguments():
    """解析命令行參數"""
    parser = argparse.ArgumentParser(description='Backpack Exchange 網格交易程序')
    
    # 基本參數
    parser.add_argument('--api-key', type=str, help='API Key (可選，默認使用環境變數或配置文件)')
    parser.add_argument('--secret-key', type=str, help='Secret Key (可選，默認使用環境變數或配置文件)')
    parser.add_argument('--cli', action='store_true', help='啟動命令行界面')
    parser.add_argument('--ws-proxy', type=str, help='WebSocket Proxy (可選，默認使用環境變數或配置文件)')
    
    # 網格交易參數
    parser.add_argument('--symbol', type=str, help='交易對 (例如: SOL_USDC)')
    parser.add_argument('--grid-upper', type=float, help='網格上限價格')
    parser.add_argument('--grid-lower', type=float, help='網格下限價格')
    parser.add_argument('--grid-num', type=int, default=10, help='網格數量 (默認: 10)')
    parser.add_argument('--quantity', type=float, help='訂單數量 (可選)')
    parser.add_argument('--auto-price', action='store_true', help='自動設置價格範圍')
    parser.add_argument('--price-range', type=float, default=5.0, help='自動模式下的價格範圍百分比 (默認: 5.0)')
    parser.add_argument('--duration', type=int, default=3600, help='運行時間（秒）(默認: 3600)')
    parser.add_argument('--interval', type=int, default=60, help='更新間隔（秒）(默認: 60)')
    
    return parser.parse_args()

def run_grid_trader(args, api_key, secret_key, ws_proxy=None):
    """運行網格交易策略"""
    # 檢查必要參數
    if not args.symbol:
        logger.error("缺少交易對參數 (--symbol)")
        return
    
    # 檢查網格參數
    if not args.auto_price and (args.grid_upper is None or args.grid_lower is None):
        logger.error("使用手動網格模式時需要指定網格上下限價格 (--grid-upper 和 --grid-lower)")
        return
    
    try:
        # 初始化網格交易
        grid_trader = GridTrader(
            api_key=api_key,
            secret_key=secret_key,
            symbol=args.symbol,
            grid_upper_price=args.grid_upper,
            grid_lower_price=args.grid_lower,
            grid_num=args.grid_num,
            order_quantity=args.quantity,
            auto_price_range=args.auto_price,
            price_range_percent=args.price_range,
            ws_proxy=ws_proxy
        )
        
        # 執行網格交易策略
        grid_trader.run(duration_seconds=args.duration, interval_seconds=args.interval)
        
    except KeyboardInterrupt:
        logger.info("收到中斷信號，正在退出...")
    except Exception as e:
        logger.error(f"網格交易過程中發生錯誤: {e}")
        import traceback
        traceback.print_exc()

def main():
    """主函數"""
    args = parse_arguments()
    
    # 優先使用命令行參數中的API密鑰
    api_key = args.api_key or API_KEY
    secret_key = args.secret_key or SECRET_KEY
    # 读取wss代理
    ws_proxy = args.ws_proxy or WS_PROXY
    
    # 檢查API密鑰
    if not api_key or not secret_key:
        logger.error("缺少API密鑰，請通過命令行參數或環境變量提供")
        sys.exit(1)
    
    # 決定執行模式
    if args.cli:
        # 啟動命令行界面
        main_cli(api_key, secret_key, ws_proxy=ws_proxy)
    elif args.symbol:
        # 如果指定了交易對，直接運行網格交易策略
        run_grid_trader(args, api_key, secret_key, ws_proxy=ws_proxy)
    else:
        # 默認啟動命令行界面
        main_cli(api_key, secret_key, ws_proxy=ws_proxy)

if __name__ == "__main__":
    main()