"""
網格交易參數助手模塊
提供自動計算和優化網格參數的功能
"""
import math
from typing import Dict, Tuple, List, Optional
from api.client import get_ticker, get_market_limits
from utils.helpers import round_to_precision, round_to_tick_size

def calculate_optimal_grid_params(
    symbol: str,
    current_price: float = None,
    volatility: float = None,
    maker_fee_rate: float = 0.001,
    tick_size: float = None,
    risk_level: str = "medium",
    order_quantity: float = None,
    min_order_size: float = None
) -> Dict:
    """
    計算最優網格參數
    
    Args:
        symbol: 交易對
        current_price: 當前價格，如果為None則會自動獲取
        volatility: 波動率，如果為None則使用預設值
        maker_fee_rate: Maker手續費率，默認0.1%
        tick_size: 價格步長，如果為None則會自動獲取
        risk_level: 風險等級 ('low', 'medium', 'high')
        order_quantity: 每格訂單數量
        min_order_size: 最小訂單大小
        
    Returns:
        包含網格參數的字典
    """
    # 獲取市場限制和價格步長
    market_limits = None
    base_asset = None
    quote_asset = None
    base_precision = None
    
    if tick_size is None or min_order_size is None:
        try:
            market_limits = get_market_limits(symbol)
            if market_limits:
                if 'tick_size' in market_limits and tick_size is None:
                    tick_size = float(market_limits['tick_size'])
                    print(f"自動獲取價格步長: {tick_size}")
                
                if 'min_order_size' in market_limits and min_order_size is None:
                    min_order_size = float(market_limits['min_order_size'])
                    print(f"自動獲取最小訂單大小: {min_order_size}")
                    
                if 'base_asset' in market_limits:
                    base_asset = market_limits['base_asset']
                    
                if 'quote_asset' in market_limits:
                    quote_asset = market_limits['quote_asset']
                    
                if 'base_precision' in market_limits:
                    base_precision = int(market_limits['base_precision'])
        except Exception as e:
            print(f"獲取市場限制出錯: {e}")
    
    # 默認值處理
    if tick_size is None:
        print("使用默認價格步長: 0.01")
        tick_size = 0.01
        
    if min_order_size is None:
        print("使用默認最小訂單大小: 0.01")
        min_order_size = 0.01
    
    # 嘗試從交易對解析資產
    if base_asset is None or quote_asset is None:
        parts = symbol.split('_')
        if len(parts) == 2:
            if base_asset is None:
                base_asset = parts[0]
            if quote_asset is None:
                quote_asset = parts[1]
    
    # 獲取當前價格（如果未提供）
    if current_price is None:
        ticker = get_ticker(symbol)
        if isinstance(ticker, dict) and "lastPrice" in ticker:
            current_price = float(ticker['lastPrice'])
        else:
            raise ValueError(f"無法獲取 {symbol} 的價格")
    
    # 根據風險等級設置參數
    risk_params = {
        "low": {"price_range_pct": 2.0, "grid_num": 6, "profit_factor": 3},
        "medium": {"price_range_pct": 4.0, "grid_num": 10, "profit_factor": 4},
        "high": {"price_range_pct": 8.0, "grid_num": 16, "profit_factor": 5}
    }
    
    params = risk_params.get(risk_level, risk_params["medium"])
    
    # 如果提供了波動率，則使用波動率調整價格範圍
    if volatility:
        # 使用波動率的2-4倍作為價格範圍
        volatility_factor = 2.0 if risk_level == "low" else 3.0 if risk_level == "medium" else 4.0
        params["price_range_pct"] = min(max(volatility * volatility_factor, 1.0), 15.0)
    
    # 計算網格間距，確保能覆蓋手續費
    total_fee_pct = maker_fee_rate * 200  # 買賣循環總手續費百分比 (0.1% * 2 * 100%)
    min_grid_gap_pct = total_fee_pct * params["profit_factor"]  # 最小網格間距百分比
    
    # 調整網格數量，確保網格間距足夠
    price_range_pct = params["price_range_pct"]
    adjusted_grid_num = int(price_range_pct / min_grid_gap_pct)
    grid_num = min(params["grid_num"], max(4, adjusted_grid_num))
    
    # 計算網格上下限
    upper_price = round_to_tick_size(current_price * (1 + price_range_pct/100), tick_size)
    lower_price = round_to_tick_size(current_price * (1 - price_range_pct/100), tick_size)
    
    # 計算實際網格間距
    actual_gap_pct = (upper_price - lower_price) / (grid_num * current_price) * 100
    profit_factor = actual_gap_pct / total_fee_pct
    
    # 檢查訂單數量是否符合最小訂單大小
    if order_quantity is not None:
        if order_quantity < min_order_size:
            print(f"警告: 設置的訂單數量 {order_quantity} 小於最小訂單大小 {min_order_size}")
            print(f"已自動調整為最小訂單大小: {min_order_size}")
            order_quantity = min_order_size
    
    # 計算所需資金
    required_base = 0  # 基礎貨幣需求
    required_quote = 0  # 報價貨幣需求
    
    if order_quantity:
        # 計算賣單所需的基礎貨幣(上半部分網格)
        sell_grids = (grid_num + 1) // 2  # 上半部分網格數（含中間網格）
        required_base = sell_grids * order_quantity
        
        # 計算買單所需的報價貨幣(下半部分網格)
        buy_grids = (grid_num + 1) - sell_grids  # 下半部分網格數
        avg_buy_price = (current_price + lower_price) / 2  # 平均買入價格
        required_quote = buy_grids * order_quantity * avg_buy_price
    
    return {
        "symbol": symbol,  # 確保包含交易對
        "grid_upper_price": upper_price,
        "grid_lower_price": lower_price,
        "grid_num": grid_num,
        "price_range_percent": price_range_pct,
        "grid_gap_percent": actual_gap_pct,
        "profit_factor": profit_factor,
        "order_quantity": order_quantity,
        "current_price": current_price,
        "tick_size": tick_size,
        "min_order_size": min_order_size,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "base_precision": base_precision,
        "required_base": required_base,
        "required_quote": required_quote,
        "estimated_profit_per_grid": f"{(actual_gap_pct - total_fee_pct):.4f}%"
    }

def print_grid_params(params: Dict):
    """
    打印網格參數詳細信息
    
    Args:
        params: 網格參數字典
    """
    print("\n=== 網格交易參數 ===")
    print(f"交易對: {params.get('symbol', 'Unknown')}")
    
    base_asset = params.get('base_asset', '')
    quote_asset = params.get('quote_asset', '')
    
    print(f"當前價格: {params['current_price']:.6f} {quote_asset}")
    print(f"網格上限: {params['grid_upper_price']:.6f} {quote_asset}")
    print(f"網格下限: {params['grid_lower_price']:.6f} {quote_asset}")
    print(f"網格數量: {params['grid_num']}")
    print(f"價格範圍: ±{params['price_range_percent']/2:.2f}%")
    print(f"網格間距: {params['grid_gap_percent']:.4f}%")
    print(f"利潤因子: {params['profit_factor']:.2f}x")
    
    if params.get('tick_size'):
        print(f"價格步長: {params['tick_size']}")
    
    if params.get('min_order_size'):
        print(f"最小訂單大小: {params['min_order_size']} {base_asset}")
    
    if params.get('order_quantity'):
        print(f"每格訂單數量: {params['order_quantity']} {base_asset}")
    
    print(f"預估每格利潤: {params['estimated_profit_per_grid']}")
    
    # 顯示所需資金
    if params.get('required_base') and params.get('required_quote'):
        print(f"\n=== 所需資金 ===")
        print(f"基礎貨幣({base_asset}): {params['required_base']:.6f}")
        print(f"報價貨幣({quote_asset}): {params['required_quote']:.6f}")
    
    # 打印網格點位
    if params['grid_num'] <= 20:  # 只在網格數量較少時打印所有點位
        price_step = (params['grid_upper_price'] - params['grid_lower_price']) / params['grid_num']
        print("\n網格價格點位:")
        for i in range(params['grid_num'] + 1):
            grid_price = params['grid_lower_price'] + i * price_step
            distance = ((grid_price / params['current_price']) - 1) * 100
            print(f"網格 {i}: {grid_price:.6f} ({distance:+.2f}% 距當前價)")

def get_risk_profile(symbol: str) -> str:
    """
    根據交易對獲取建議的風險級別
    
    Args:
        symbol: 交易對符號
        
    Returns:
        風險級別 ('low', 'medium', 'high')
    """
    # 常見穩定幣交易對
    stable_pairs = ['USDT_USDC', 'USDC_USDT', 'USDT_DAI', 'DAI_USDC', 'BUSD_USDT', 'BUSD_USDC']
    
    # 中等波動性資產
    medium_volatility = ['BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOT']
    
    # 穩定幣交易對使用低風險設置
    if symbol in stable_pairs:
        return "low"
    
    # 檢查交易對中的資產
    for asset in medium_volatility:
        if asset in symbol:
            return "medium"
    
    # 默認使用高風險設置
    return "high"

def interactive_setup():
    """
    交互式設置網格參數
    
    Returns:
        網格參數字典
    """
    print("\n=== 網格交易參數助手 ===")
    symbol = input("請輸入交易對 (例如: SOL_USDC): ").strip()
    
    # 獲取市場限制
    tick_size = None
    min_order_size = None
    base_asset = None
    quote_asset = None
    base_precision = None
    
    try:
        market_limits = get_market_limits(symbol)
        if market_limits:
            if 'tick_size' in market_limits:
                tick_size = float(market_limits['tick_size'])
                print(f"自動獲取價格步長: {tick_size}")
            
            if 'min_order_size' in market_limits:
                min_order_size = float(market_limits['min_order_size'])
                print(f"自動獲取最小訂單大小: {min_order_size}")
                
            if 'base_asset' in market_limits:
                base_asset = market_limits['base_asset']
                print(f"基礎貨幣: {base_asset}")
                
            if 'quote_asset' in market_limits:
                quote_asset = market_limits['quote_asset']
                print(f"報價貨幣: {quote_asset}")
                
            if 'base_precision' in market_limits:
                base_precision = int(market_limits['base_precision'])
    except Exception as e:
        print(f"獲取市場限制出錯: {e}")
        # 嘗試從交易對名稱解析資產
        if '_' in symbol:
            base_asset = symbol.split('_')[0]
            quote_asset = symbol.split('_')[1]
    
    # 設置默認值
    if tick_size is None:
        tick_size_input = input("自動獲取價格步長失敗，請手動輸入 (默認0.01): ").strip()
        tick_size = float(tick_size_input) if tick_size_input else 0.01
        
    if min_order_size is None:
        min_order_size_input = input("自動獲取最小訂單大小失敗，請手動輸入 (默認0.01): ").strip()
        min_order_size = float(min_order_size_input) if min_order_size_input else 0.01
    
    # 獲取當前價格
    try:
        ticker = get_ticker(symbol)
        if isinstance(ticker, dict) and "lastPrice" in ticker:
            current_price = float(ticker['lastPrice'])
            print(f"當前價格: {current_price} {quote_asset or ''}")
        else:
            current_price = float(input("無法自動獲取價格，請手動輸入當前價格: "))
    except:
        current_price = float(input("無法自動獲取價格，請手動輸入當前價格: "))
    
    # 指定每格訂單數量
    quantity_prompt = f"請輸入每格訂單數量 (最小 {min_order_size} {base_asset or ''}): "
    quantity_input = input(quantity_prompt).strip()
    
    if quantity_input:
        order_quantity = float(quantity_input)
        # 檢查是否滿足最小訂單大小
        if order_quantity < min_order_size:
            print(f"警告: 訂單數量 {order_quantity} 小於最小限制 {min_order_size}，已自動調整")
            order_quantity = min_order_size
    else:
        order_quantity = None
    
    # 獲取手續費率
    fee_input = input("請輸入Maker手續費率 (%, 默認0.1): ").strip()
    maker_fee_rate = float(fee_input) / 100 if fee_input else 0.001
    
    # 獲取風險級別
    default_risk = get_risk_profile(symbol)
    risk_input = input(f"選擇風險級別 (low/medium/high, 默認 {default_risk}): ").strip().lower()
    risk_level = risk_input if risk_input in ["low", "medium", "high"] else default_risk

    # 獲取策略執行時間與間隔
    duration_input = input("請輸入策略執行時間（秒, 默認3600）: ").strip()
    duration = int(duration_input) if duration_input else 3600

    interval_input = input("請輸入執行間隔（秒, 默認60）: ").strip()
    interval = int(interval_input) if interval_input else 60
    
    # 計算最優參數
    params = calculate_optimal_grid_params(
        symbol,
        current_price=current_price,
        maker_fee_rate=maker_fee_rate,
        tick_size=tick_size,
        risk_level=risk_level,
        order_quantity=order_quantity,
        min_order_size=min_order_size
    )
    
    # 打印參數
    print_grid_params(params)
    
    # 詢問是否確認
    confirm = input("\n是否使用這些參數? (y/n): ").strip().lower()
    
    if confirm == 'y':
        params['duration'] = duration
        params['interval'] = interval
        return params
    else:
        # 允許手動調整參數
        print("\n--- 手動調整參數 ---")
        range_input = input(f"價格範圍百分比 (默認 {params['price_range_percent']}): ").strip()
        price_range = float(range_input) if range_input else params['price_range_percent']
        
        grid_num_input = input(f"網格數量 (默認 {params['grid_num']}): ").strip()
        grid_num = int(grid_num_input) if grid_num_input else params['grid_num']
        
        quantity_input = input(f"每格訂單數量 (默認 {params.get('order_quantity') or '自動'}): ").strip()
        if quantity_input:
            order_quantity = float(quantity_input)
            # 檢查是否滿足最小訂單大小
            if order_quantity < min_order_size:
                print(f"警告: 訂單數量 {order_quantity} 小於最小限制 {min_order_size}，已自動調整")
                order_quantity = min_order_size
        else:
            order_quantity = params.get('order_quantity')
        
        # 重新計算
        upper_price = round_to_tick_size(current_price * (1 + price_range/100), tick_size)
        lower_price = round_to_tick_size(current_price * (1 - price_range/100), tick_size)
        actual_gap_pct = (upper_price - lower_price) / (grid_num * current_price) * 100
        total_fee_pct = maker_fee_rate * 200
        profit_factor = actual_gap_pct / total_fee_pct
        
        # 計算所需資金
        required_base = 0
        required_quote = 0
        
        if order_quantity:
            # 計算賣單所需的基礎貨幣(上半部分網格)
            sell_grids = (grid_num + 1) // 2  # 上半部分網格數（含中間網格）
            required_base = sell_grids * order_quantity
            
            # 計算買單所需的報價貨幣(下半部分網格)
            buy_grids = (grid_num + 1) - sell_grids  # 下半部分網格數
            avg_buy_price = (current_price + lower_price) / 2  # 平均買入價格
            required_quote = buy_grids * order_quantity * avg_buy_price
        
        adjusted_params = {
            "symbol": symbol,
            "grid_upper_price": upper_price,
            "grid_lower_price": lower_price,
            "grid_num": grid_num,
            "price_range_percent": price_range,
            "grid_gap_percent": actual_gap_pct,
            "profit_factor": profit_factor,
            "order_quantity": order_quantity,
            "current_price": current_price,
            "tick_size": tick_size,
            "min_order_size": min_order_size,
            "base_asset": base_asset,
            "quote_asset": quote_asset,
            "base_precision": base_precision,
            "required_base": required_base,
            "required_quote": required_quote,
            "estimated_profit_per_grid": f"{(actual_gap_pct - total_fee_pct):.4f}%",
            "duration": duration,
            "interval": interval

        }
        
        print("\n--- 調整後參數 ---")
        print_grid_params(adjusted_params)
        
        return adjusted_params

if __name__ == "__main__":
    # 獨立運行時啟動交互式設置
    params = interactive_setup()
    
    # 打印可直接使用的命令行
    cmd = f"python run.py --symbol {params['symbol']} --grid-upper {params['grid_upper_price']} --grid-lower {params['grid_lower_price']} --grid-num {params['grid_num']}"
    if params.get('order_quantity'):
        cmd += f" --quantity {params['order_quantity']}"
    if 'duration' in params:
        cmd += f" --duration {params['duration']}"
    if 'interval' in params:
        cmd += f" --interval {params['interval']}"
    
    print("\n=== 可直接使用的命令 ===")
    print(cmd)