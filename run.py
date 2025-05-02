#!/usr/bin/env python
"""
Backpack Exchange 網格交易程序統一入口
支持命令行模式和智能參數設置
"""
import argparse
import sys
import os

# 嘗試導入需要的模塊
try:
    from logger import setup_logger
    from config import API_KEY, SECRET_KEY, WS_PROXY
    from utils.grid_helper import interactive_setup, calculate_optimal_grid_params, print_grid_params
except ImportError:
    API_KEY = os.getenv('API_KEY')
    SECRET_KEY = os.getenv('SECRET_KEY')
    WS_PROXY = os.getenv('PROXY_WEBSOCKET')
    
    def setup_logger(name):
        import logging
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)
        return logger
    
    print("無法導入網格助手模塊，將使用基本參數設置")

# 創建記錄器
logger = setup_logger("main")

def parse_arguments():
    """解析命令行參數"""
    parser = argparse.ArgumentParser(description='Backpack Exchange 網格交易程序')
    
    # 模式選擇
    parser.add_argument('--cli', action='store_true', help='啟動命令行界面 (默認模式)')
    parser.add_argument('--setup', action='store_true', help='啟動交互式參數設置助手')
    parser.add_argument('--smart', action='store_true', help='使用智能參數計算')
    
    # 基本參數
    parser.add_argument('--api-key', type=str, help='API Key (可選，默認使用環境變數或配置文件)')
    parser.add_argument('--secret-key', type=str, help='Secret Key (可選，默認使用環境變數或配置文件)')
    parser.add_argument('--ws-proxy', type=str, help='WebSocket Proxy (可選，默認使用環境變數或配置文件)')
    
    # 網格交易參數
    parser.add_argument('--symbol', type=str, help='交易對 (例如: SOL_USDC)')
    parser.add_argument('--grid-upper', type=float, help='網格上限價格')
    parser.add_argument('--grid-lower', type=float, help='網格下限價格')
    parser.add_argument('--grid-num', type=int, default=10, help='網格數量 (默認: 10)')
    parser.add_argument('--quantity', type=float, help='每格訂單數量 (可選)')
    parser.add_argument('--auto-price', action='store_true', help='自動設置價格範圍')
    parser.add_argument('--price-range', type=float, default=5.0, help='自動模式下的價格範圍百分比 (默認: 5.0)')
    parser.add_argument('--duration', type=int, default=3600, help='運行時間（秒）(默認: 3600)')
    parser.add_argument('--interval', type=int, default=60, help='更新間隔（秒）(默認: 60)')
    
    # 額外參數
    parser.add_argument('--fee-rate', type=float, default=0.1, help='Maker手續費率百分比 (默認: 0.1)')
    parser.add_argument('--risk', type=str, choices=['low', 'medium', 'high'], default='medium', 
                        help='風險等級 (low/medium/high, 默認: medium)')

    return parser.parse_args()

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
    if args.setup:
        # 啟動交互式參數設置助手
        try:
            params = interactive_setup()
            print("\n設置完成！可以使用以下命令運行網格交易：")
            cmd = f"python run.py --symbol {params['symbol']} --grid-upper {params['grid_upper_price']} --grid-lower {params['grid_lower_price']} --grid-num {params['grid_num']}"
            if params.get('order_quantity'):
                cmd += f" --quantity {params['order_quantity']}"
            if 'duration' in params:
                cmd += f" --duration {params['duration']}"
            if 'interval' in params:
                cmd += f" --interval {params['interval']}"
            print(cmd)
            
            # 詢問是否立即運行
            run_now = input("\n是否立即運行？(y/n): ").strip().lower()
            if run_now != 'y':
                return
                
            # 更新參數
            args.symbol = params['symbol']
            args.grid_upper = params['grid_upper_price']
            args.grid_lower = params['grid_lower_price']
            args.grid_num = params['grid_num']
            if params.get('order_quantity'):
                args.quantity = params['order_quantity']
            if 'duration' in params:
                args.duration = params['duration']
            if 'interval' in params:
                args.interval = params['interval']
            
        except ImportError:
            logger.error("無法找到參數設置助手模塊，請安裝必要的依賴")
            return
        except Exception as e:
            logger.error(f"參數設置過程中發生錯誤: {e}")
            return
    
    if args.smart and args.symbol:
        # 使用智能參數計算
        try:
            maker_fee_rate = args.fee_rate / 100
            params = calculate_optimal_grid_params(
                args.symbol,
                maker_fee_rate=maker_fee_rate,
                risk_level=args.risk
            )
            print_grid_params(params)
            
            # 詢問是否使用這些參數
            confirm = input("\n是否使用這些參數？(y/n): ").strip().lower()
            if confirm != 'y':
                return
                
            # 更新參數
            args.grid_upper = params['grid_upper_price']
            args.grid_lower = params['grid_lower_price']
            args.grid_num = params['grid_num']
            if params.get('order_quantity'):
                args.quantity = params['order_quantity']
            
        except ImportError:
            logger.error("無法找到智能參數計算模塊，請安裝必要的依賴")
            return
        except Exception as e:
            logger.error(f"智能參數計算過程中發生錯誤: {e}")
            return
    
    if args.symbol:
        # 如果指定了交易對，直接運行網格交易策略
        try:
            from strategies.grid_trader import GridTrader
            
            # 檢查網格參數
            if not args.auto_price and (args.grid_upper is None or args.grid_lower is None):
                logger.error("使用手動網格模式時需要指定網格上下限價格 (--grid-upper 和 --grid-lower)")
                return
            
            # 確保數量參數被正確傳遞
            if args.quantity is None:
                logger.warning("未指定訂單數量，將使用交易所最小訂單大小")
            else:
                logger.info(f"使用訂單數量: {args.quantity}")
            
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
    else:
        # 默認啟動命令行界面
        try:
            from cli.commands import main_cli
            main_cli(api_key, secret_key, ws_proxy=ws_proxy)
        except ImportError as e:
            logger.error(f"啟動命令行界面時出錯: {str(e)}")
            sys.exit(1)

if __name__ == "__main__":
    main()