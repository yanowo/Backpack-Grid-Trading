"""
網格交易策略模塊
"""
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Union, Any
from concurrent.futures import ThreadPoolExecutor

from api.client import (
    get_balance, execute_order, get_open_orders, cancel_all_orders, 
    cancel_order, get_market_limits, get_klines, get_ticker, get_order_book
)
from ws_client.client import BackpackWebSocket
from database.db import Database
from utils.helpers import round_to_precision, round_to_tick_size, calculate_volatility
from logger import setup_logger

logger = setup_logger("grid_trader")

class GridTrader:
    def __init__(
        self, 
        api_key, 
        secret_key, 
        symbol, 
        db_instance=None,
        grid_upper_price=None,  # 網格上限價格
        grid_lower_price=None,  # 網格下限價格
        grid_num=10,           # 網格數量
        order_quantity=None,    # 每格訂單數量
        auto_price_range=True,  # 自動設置價格範圍
        price_range_percent=5.0, # 自動模式下的價格範圍百分比
        ws_proxy=None,
        max_position=0.5       # 最大持倉量限制
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.symbol = symbol
        self.grid_num = grid_num
        self.order_quantity = order_quantity
        self.auto_price_range = auto_price_range
        self.price_range_percent = price_range_percent
        self.grid_upper_price = grid_upper_price
        self.grid_lower_price = grid_lower_price
        self.max_position = max_position
        
        # 網格交易狀態
        self.grid_initialized = False
        
        # 舊的網格訂單跟蹤結構（保留向後兼容）
        self.grid_orders = {}  # 保存網格訂單 {網格價格: 訂單信息}
        self.grid_buy_orders = {}  # 買入網格訂單
        self.grid_sell_orders = {}  # 賣出網格訂單
        
        # 新的網格訂單跟蹤結構
        self.grid_orders_by_price = {}  # {價格: [訂單信息1, 訂單信息2, ...]}
        self.grid_orders_by_id = {}     # {訂單ID: 訂單信息}
        self.grid_buy_orders_by_price = {}  # {價格: [買單信息1, 買單信息2, ...]}
        self.grid_sell_orders_by_price = {}  # {價格: [賣單信息1, 賣單信息2, ...]}
        
        # 添加網格點位狀態跟蹤
        self.grid_status = {}  # {價格: 狀態} 狀態可以是 'buy_placed', 'buy_filled', 'sell_placed', 'sell_filled'
        self.grid_dependencies = {}  # {價格: 依賴價格} 記錄點位之間的依賴關係
        
        self.grid_levels = []  # 保存所有網格價格點位
        
        # 初始化數據庫
        self.db = db_instance if db_instance else Database()
        
        # 統計屬性
        self.session_start_time = datetime.now()
        self.session_buy_trades = []
        self.session_sell_trades = []
        self.session_fees = 0.0
        self.session_maker_buy_volume = 0.0
        self.session_maker_sell_volume = 0.0
        self.session_taker_buy_volume = 0.0
        self.session_taker_sell_volume = 0.0
        
        # 初始化市場限制
        self.market_limits = get_market_limits(symbol)
        if not self.market_limits:
            raise ValueError(f"無法獲取 {symbol} 的市場限制")
        
        self.base_asset = self.market_limits['base_asset']
        self.quote_asset = self.market_limits['quote_asset']
        self.base_precision = self.market_limits['base_precision']
        self.quote_precision = self.market_limits['quote_precision']
        self.min_order_size = float(self.market_limits['min_order_size'])
        self.tick_size = float(self.market_limits['tick_size'])
        
        # 交易量統計
        self.maker_buy_volume = 0
        self.maker_sell_volume = 0
        self.taker_buy_volume = 0
        self.taker_sell_volume = 0
        self.total_fees = 0

        # 添加代理參數
        self.ws_proxy = ws_proxy
        # 建立WebSocket連接
        self.ws = BackpackWebSocket(api_key, secret_key, symbol, self.on_ws_message, auto_reconnect=True, proxy=self.ws_proxy)
        self.ws.connect()
        
        # 記錄買賣數量以便跟蹤
        self.total_bought = 0
        self.total_sold = 0
        
        # 交易記錄 - 用於計算利潤
        self.buy_trades = []
        self.sell_trades = []
        
        # 利潤統計
        self.total_profit = 0
        self.trades_executed = 0
        self.orders_placed = 0
        self.orders_cancelled = 0
        
        # 執行緒池用於後台任務
        self.executor = ThreadPoolExecutor(max_workers=3)
        
        # 等待WebSocket連接建立並進行初始化訂閲
        self._initialize_websocket()
        
        # 載入交易統計和歷史交易
        self._load_trading_stats()
        self._load_recent_trades()
        
        logger.info(f"初始化網格交易: {symbol}")
        logger.info(f"基礎資產: {self.base_asset}, 報價資產: {self.quote_asset}")
        logger.info(f"基礎精度: {self.base_precision}, 報價精度: {self.quote_precision}")
        logger.info(f"最小訂單大小: {self.min_order_size}, 價格步長: {self.tick_size}")
        logger.info(f"網格數量: {self.grid_num}")
        logger.info(f"最大持倉限制: {self.max_position} {self.base_asset}")
    
    def _initialize_websocket(self):
        """等待WebSocket連接建立並進行初始化訂閲"""
        wait_time = 0
        max_wait_time = 10
        while not self.ws.connected and wait_time < max_wait_time:
            time.sleep(0.5)
            wait_time += 0.5
        
        if self.ws.connected:
            logger.info("WebSocket連接已建立，初始化數據流...")
            
            # 初始化訂單簿
            orderbook_initialized = self.ws.initialize_orderbook()
            
            # 訂閲深度流和行情數據
            if orderbook_initialized:
                depth_subscribed = self.ws.subscribe_depth()
                ticker_subscribed = self.ws.subscribe_bookTicker()
                
                if depth_subscribed and ticker_subscribed:
                    logger.info("數據流訂閲成功!")
            
            # 訂閲私有訂單更新流
            self.subscribe_order_updates()
        else:
            logger.warning(f"WebSocket連接建立超時，將在運行過程中繼續嘗試連接")
    
    def _load_trading_stats(self):
        """從數據庫加載交易統計數據"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            
            # 查詢今天的統計數據
            stats = self.db.get_trading_stats(self.symbol, today)
            
            if stats and len(stats) > 0:
                stat = stats[0]
                self.maker_buy_volume = stat['maker_buy_volume']
                self.maker_sell_volume = stat['maker_sell_volume']
                self.taker_buy_volume = stat['taker_buy_volume']
                self.taker_sell_volume = stat['taker_sell_volume']
                self.total_profit = stat['realized_profit']
                self.total_fees = stat['total_fees']
                
                logger.info(f"已從數據庫加載今日交易統計")
                logger.info(f"Maker買入量: {self.maker_buy_volume}, Maker賣出量: {self.maker_sell_volume}")
                logger.info(f"Taker買入量: {self.taker_buy_volume}, Taker賣出量: {self.taker_sell_volume}")
                logger.info(f"已實現利潤: {self.total_profit}, 總手續費: {self.total_fees}")
            else:
                logger.info("今日無交易統計記錄，將創建新記錄")
        except Exception as e:
            logger.error(f"加載交易統計時出錯: {e}")
    
    def _load_recent_trades(self):
        """從數據庫加載歷史成交記錄"""
        try:
            # 獲取訂單歷史
            trades = self.db.get_order_history(self.symbol, 1000)
            trades_count = len(trades) if trades else 0
            
            if trades_count > 0:
                for side, quantity, price, maker, fee in trades:
                    quantity = float(quantity)
                    price = float(price)
                    fee = float(fee)
                    
                    if side == 'Bid':  # 買入
                        self.buy_trades.append((price, quantity))
                        self.total_bought += quantity
                        if maker:
                            self.maker_buy_volume += quantity
                        else:
                            self.taker_buy_volume += quantity
                    elif side == 'Ask':  # 賣出
                        self.sell_trades.append((price, quantity))
                        self.total_sold += quantity
                        if maker:
                            self.maker_sell_volume += quantity
                        else:
                            self.taker_sell_volume += quantity
                    
                    self.total_fees += fee
                
                logger.info(f"已從數據庫載入 {trades_count} 條歷史成交記錄")
                logger.info(f"總買入: {self.total_bought} {self.base_asset}, 總賣出: {self.total_sold} {self.base_asset}")
                logger.info(f"Maker買入: {self.maker_buy_volume} {self.base_asset}, Maker賣出: {self.maker_sell_volume} {self.base_asset}")
                logger.info(f"Taker買入: {self.taker_buy_volume} {self.base_asset}, Taker賣出: {self.taker_sell_volume} {self.base_asset}")
                
                # 計算精確利潤
                self.total_profit = self._calculate_db_profit()
                logger.info(f"計算得出已實現利潤: {self.total_profit:.8f} {self.quote_asset}")
                logger.info(f"總手續費: {self.total_fees:.8f} {self.quote_asset}")
            else:
                logger.info("數據庫中沒有歷史成交記錄，嘗試從API獲取")
                self._load_trades_from_api()
                
        except Exception as e:
            logger.error(f"載入歷史成交記錄時出錯: {e}")
            import traceback
            traceback.print_exc()
    
    def _initialize_dependencies_from_history(self):
        """根據歷史交易初始化網格依賴關係"""
        logger.info("根據歷史交易初始化網格依賴關係...")
        
        # 獲取所有歷史交易，不僅僅是10筆
        recent_trades = self.db.get_recent_trades(self.symbol, 100)  # 獲取更多歷史交易
        
        # 按時間排序
        if recent_trades:
            recent_trades.sort(key=lambda x: x['timestamp'])
        
        # 跟蹤每個網格點位的最新狀態
        for trade in recent_trades:
            price = float(trade['price'])
            side = trade['side']
            
            # 找到最接近的網格點位
            grid_price = min(self.grid_levels, key=lambda x: abs(x - price))
            
            if side == 'Bid':  # 買入成交
                # 找到下一個更高的網格點位
                next_price = None
                for p in sorted(self.grid_levels):
                    if p > grid_price:
                        next_price = p
                        break
                        
                if next_price:
                    # 設置狀態和依賴關係
                    self.grid_status[grid_price] = 'buy_filled'
                    self.grid_status[next_price] = 'sell_placed'
                    self.grid_dependencies[grid_price] = next_price
                    
            elif side == 'Ask':  # 賣出成交
                # 解除依賴關係
                for price, dependent_price in list(self.grid_dependencies.items()):
                    if dependent_price == grid_price:
                        del self.grid_dependencies[price]
        
        # 記錄當前依賴關係
        dependency_count = len(self.grid_dependencies)
        if dependency_count > 0:
            logger.info(f"已從歷史交易建立 {dependency_count} 個網格依賴關係")
            for price, dependent_price in self.grid_dependencies.items():
                logger.info(f"  價格點位 {price} 依賴於 {dependent_price} 的賣單成交")
    
    def _load_trades_from_api(self):
        """從API加載歷史成交記錄"""
        from api.client import get_fill_history
        
        fill_history = get_fill_history(self.api_key, self.secret_key, self.symbol, 100)
        
        if isinstance(fill_history, dict) and "error" in fill_history:
            logger.error(f"載入成交記錄失敗: {fill_history['error']}")
            return
            
        if not fill_history:
            logger.info("沒有找到歷史成交記錄")
            return
        
        # 批量插入準備
        for fill in fill_history:
            price = float(fill.get('price', 0))
            quantity = float(fill.get('quantity', 0))
            side = fill.get('side')
            maker = fill.get('maker', False)
            fee = float(fill.get('fee', 0))
            fee_asset = fill.get('feeAsset', '')
            order_id = fill.get('orderId', '')
            
            # 準備訂單數據
            order_data = {
                'order_id': order_id,
                'symbol': self.symbol,
                'side': side,
                'quantity': quantity,
                'price': price,
                'maker': maker,
                'fee': fee,
                'fee_asset': fee_asset,
                'trade_type': 'manual'
            }
            
            # 插入數據庫
            self.db.insert_order(order_data)
            
            if side == 'Bid':  # 買入
                self.buy_trades.append((price, quantity))
                self.total_bought += quantity
                if maker:
                    self.maker_buy_volume += quantity
                else:
                    self.taker_buy_volume += quantity
            elif side == 'Ask':  # 賣出
                self.sell_trades.append((price, quantity))
                self.total_sold += quantity
                if maker:
                    self.maker_sell_volume += quantity
                else:
                    self.taker_sell_volume += quantity
            
            self.total_fees += fee
        
        if fill_history:
            logger.info(f"已從API載入並存儲 {len(fill_history)} 條歷史成交記錄")
            
            # 更新總計
            logger.info(f"總買入: {self.total_bought} {self.base_asset}, 總賣出: {self.total_sold} {self.base_asset}")
            logger.info(f"Maker買入: {self.maker_buy_volume} {self.base_asset}, Maker賣出: {self.maker_sell_volume} {self.base_asset}")
            logger.info(f"Taker買入: {self.taker_buy_volume} {self.base_asset}, Taker賣出: {self.taker_sell_volume} {self.base_asset}")
            
            # 計算精確利潤
            self.total_profit = self._calculate_db_profit()
            logger.info(f"計算得出已實現利潤: {self.total_profit:.8f} {self.quote_asset}")
            logger.info(f"總手續費: {self.total_fees:.8f} {self.quote_asset}")
    
    def check_ws_connection(self):
        """檢查並恢復WebSocket連接"""
        ws_connected = self.ws and self.ws.is_connected()
        
        if not ws_connected:
            logger.warning("WebSocket連接已斷開或不可用，嘗試重新連接...")
            
            # 嘗試關閉現有連接
            if self.ws:
                try:
                    # 使用 BackpackWebSocket 的 close 方法以確保完全斷開
                    self.ws.close()
                    time.sleep(0.5)
                except Exception as e:
                    logger.error(f"關閉現有WebSocket時出錯: {e}")
            
            # 創建新的連接
            try:
                logger.info("創建新的WebSocket連接...")
                self.ws = BackpackWebSocket(
                    self.api_key, 
                    self.secret_key, 
                    self.symbol, 
                    self.on_ws_message, 
                    auto_reconnect=True,
                    proxy=self.ws_proxy
                )
                self.ws.connect()
                
                # 等待連接建立
                wait_time = 0
                max_wait_time = 5
                while not self.ws.is_connected() and wait_time < max_wait_time:
                    time.sleep(0.5)
                    wait_time += 0.5
                    
                if self.ws.is_connected():
                    logger.info("WebSocket重新連接成功")
                    
                    # 重新初始化
                    self.ws.initialize_orderbook()
                    self.ws.subscribe_depth()
                    self.ws.subscribe_bookTicker()
                    self.subscribe_order_updates()
                else:
                    logger.warning("WebSocket重新連接嘗試中，將在下次迭代再次檢查")
                    
            except Exception as e:
                logger.error(f"創建新WebSocket連接時出錯: {e}")
                return False
        
        return self.ws and self.ws.is_connected()
    
    def on_ws_message(self, stream, data):
        """處理WebSocket消息回調"""
        if stream.startswith("account.orderUpdate."):
            event_type = data.get('e')
            
            # 「訂單成交」事件
            if event_type == 'orderFill':
                try:
                    side = data.get('S')
                    quantity = float(data.get('l', '0'))  # 此次成交數量
                    price = float(data.get('L', '0'))     # 此次成交價格
                    order_id = data.get('i')             # 訂單 ID
                    maker = data.get('m', False)         # 是否是 Maker
                    fee = float(data.get('n', '0'))      # 手續費
                    fee_asset = data.get('N', '')        # 手續費資產

                    logger.info(f"訂單成交: ID={order_id}, 方向={side}, 數量={quantity}, 價格={price}, Maker={maker}, 手續費={fee:.8f}")
                    
                    # 查找是哪個網格點位的訂單
                    # 優先使用新結構
                    order_info = self.grid_orders_by_id.get(order_id)
                    
                    if order_info:
                        # 使用新的訂單結構
                        grid_price = order_info['price']
                        logger.info(f"網格交易: 價格 {grid_price} 的訂單已成交")
                        
                        # 從訂單跟蹤結構中移除此訂單
                        self._remove_order(order_id, grid_price, side)
                        
                        if side == 'Bid':  # 買單成交
                            # 在下一個更高的網格點位創建賣單
                            self._place_sell_order_after_buy(price, grid_price, quantity, fee if fee_asset == self.base_asset else None)
                        elif side == 'Ask':  # 賣單成交
                            # 在下一個更低的網格點位創建買單
                            self._place_buy_order_after_sell(price, grid_price, quantity, fee if fee_asset == self.quote_asset else None)
                    else:
                        # 嘗試使用舊的訂單結構
                        found = False
                        for grid_price, old_order_info in list(self.grid_orders.items()):
                            if old_order_info.get('order_id') == order_id:
                                logger.info(f"網格交易(舊結構): 價格 {grid_price} 的訂單已成交")
                                # 從當前網格訂單中移除
                                self.grid_orders.pop(grid_price, None)
                                if side == 'Bid':  # 買單成交
                                    self.grid_buy_orders.pop(grid_price, None)
                                    # 在下一個更高的網格點位創建賣單
                                    self._place_sell_order_after_buy(price, grid_price, quantity, fee if fee_asset == self.base_asset else None)
                                elif side == 'Ask':  # 賣單成交
                                    self.grid_sell_orders.pop(grid_price, None)
                                    # 在下一個更低的網格點位創建買單
                                    self._place_buy_order_after_sell(price, grid_price, quantity, fee if fee_asset == self.quote_asset else None)
                                found = True
                                break
                        
                        if not found:
                            logger.warning(f"找不到與訂單ID={order_id}匹配的網格訂單")
                    
                    # 判斷交易類型
                    trade_type = 'grid_trading'  # 默認為網格交易
                    
                    # 準備訂單數據
                    order_data = {
                        'order_id': order_id,
                        'symbol': self.symbol,
                        'side': side,
                        'quantity': quantity,
                        'price': price,
                        'maker': maker,
                        'fee': fee,
                        'fee_asset': fee_asset,
                        'trade_type': trade_type
                    }
                    
                    # 安全地插入數據庫
                    def safe_insert_order():
                        try:
                            self.db.insert_order(order_data)
                        except Exception as db_err:
                            logger.error(f"插入訂單數據時出錯: {db_err}")
                    
                    # 直接在當前線程中插入訂單數據，確保先寫入基本數據
                    safe_insert_order()
                    
                    # 更新買賣量和做市商成交量統計
                    if side == 'Bid':  # 買入
                        self.total_bought += quantity
                        self.buy_trades.append((price, quantity))
                        logger.info(f"買入成交: {quantity} {self.base_asset} @ {price} {self.quote_asset}")
                        
                        # 更新做市商成交量
                        if maker:
                            self.maker_buy_volume += quantity
                            self.session_maker_buy_volume += quantity
                        else:
                            self.taker_buy_volume += quantity
                            self.session_taker_buy_volume += quantity
                        
                        self.session_buy_trades.append((price, quantity))
                            
                    elif side == 'Ask':  # 賣出
                        self.total_sold += quantity
                        self.sell_trades.append((price, quantity))
                        logger.info(f"賣出成交: {quantity} {self.base_asset} @ {price} {self.quote_asset}")
                        
                        # 更新做市商成交量
                        if maker:
                            self.maker_sell_volume += quantity
                            self.session_maker_sell_volume += quantity
                        else:
                            self.taker_sell_volume += quantity
                            self.session_taker_sell_volume += quantity
                            
                        self.session_sell_trades.append((price, quantity))
                    
                    # 更新累計手續費
                    self.total_fees += fee
                    self.session_fees += fee
                        
                    # 在單獨的線程中更新統計數據，避免阻塞主回調
                    def safe_update_stats_wrapper():
                        try:
                            self._update_trading_stats()
                        except Exception as e:
                            logger.error(f"更新交易統計時出錯: {e}")
                    
                    self.executor.submit(safe_update_stats_wrapper)
                    
                    # 重新計算利潤（基於數據庫記錄）
                    # 也在單獨的線程中進行計算，避免阻塞
                    def update_profit():
                        try:
                            profit = self._calculate_db_profit()
                            self.total_profit = profit
                        except Exception as e:
                            logger.error(f"更新利潤計算時出錯: {e}")
                    
                    self.executor.submit(update_profit)
                    
                    # 計算本次執行的簡單利潤（不涉及數據庫查詢）
                    session_profit = self._calculate_session_profit()
                    
                    # 執行簡要統計
                    logger.info(f"累計利潤: {self.total_profit:.8f} {self.quote_asset}")
                    logger.info(f"本次執行利潤: {session_profit:.8f} {self.quote_asset}")
                    logger.info(f"本次執行手續費: {self.session_fees:.8f} {self.quote_asset}")
                    logger.info(f"本次執行淨利潤: {(session_profit - self.session_fees):.8f} {self.quote_asset}")
                    
                    self.trades_executed += 1
                    logger.info(f"總買入: {self.total_bought} {self.base_asset}, 總賣出: {self.total_sold} {self.base_asset}")
                    logger.info(f"Maker買入: {self.maker_buy_volume} {self.base_asset}, Maker賣出: {self.maker_sell_volume} {self.base_asset}")
                    logger.info(f"Taker買入: {self.taker_buy_volume} {self.base_asset}, Taker賣出: {self.taker_sell_volume} {self.base_asset}")
                    
                except Exception as e:
                    logger.error(f"處理訂單成交消息時出錯: {e}")
                    import traceback
                    traceback.print_exc()
    
    def _remove_order(self, order_id, grid_price, side):
        """從訂單跟蹤結構中移除訂單"""
        # 從ID字典中移除
        if order_id in self.grid_orders_by_id:
            del self.grid_orders_by_id[order_id]
        
        # 從價格字典中移除
        if grid_price in self.grid_orders_by_price:
            self.grid_orders_by_price[grid_price] = [order for order in self.grid_orders_by_price[grid_price] 
                                                  if order.get('order_id') != order_id]
            # 如果此價格沒有訂單了，清除此項
            if not self.grid_orders_by_price[grid_price]:
                del self.grid_orders_by_price[grid_price]
        
        # 從相應的買賣單字典中移除
        if side == 'Bid' and grid_price in self.grid_buy_orders_by_price:
            self.grid_buy_orders_by_price[grid_price] = [order for order in self.grid_buy_orders_by_price[grid_price] 
                                                      if order.get('order_id') != order_id]
            if not self.grid_buy_orders_by_price[grid_price]:
                del self.grid_buy_orders_by_price[grid_price]
        
        elif side == 'Ask' and grid_price in self.grid_sell_orders_by_price:
            self.grid_sell_orders_by_price[grid_price] = [order for order in self.grid_sell_orders_by_price[grid_price] 
                                                       if order.get('order_id') != order_id]
            if not self.grid_sell_orders_by_price[grid_price]:
                del self.grid_sell_orders_by_price[grid_price]
    
    def _place_sell_order_after_buy(self, executed_price, grid_price, quantity, actual_fee=None):
        """買單成交後在上一個網格點位放置賣單"""
        # 找到買入價格的下一個更高網格點位
        next_price = None
        for price in sorted(self.grid_levels):
            if price > grid_price:
                next_price = price
                break
        
        if next_price:
            logger.info(f"在網格點位 {next_price} 放置賣單 (買入價格: {executed_price})")
            
            # 調整數量，考慮到實際手續費
            if actual_fee is not None and isinstance(actual_fee, (int, float)):
                # 實際手續費（確認單位是否為SOL）
                if actual_fee > 0 and actual_fee < quantity:  # 合理的手續費範圍
                    adjusted_quantity = round_to_precision(quantity - actual_fee, self.base_precision)
                else:
                    adjusted_quantity = round_to_precision(quantity, self.base_precision)
            else:
                # 如果沒有傳入有效的實際手續費，使用估計值
                adjusted_quantity = round_to_precision(quantity * 0.999, self.base_precision)
            
            # 檢查最小訂單大小
            if adjusted_quantity < self.min_order_size:
                logger.info(f"調整後數量 {adjusted_quantity} 低於最小訂單大小，使用最小值 {self.min_order_size}")
                adjusted_quantity = self.min_order_size
            
            # 放置賣單
            order_details = {
                "orderType": "Limit",
                "price": str(next_price),
                "quantity": str(adjusted_quantity),
                "side": "Ask",
                "symbol": self.symbol,
                "timeInForce": "GTC",
                "postOnly": True
            }
            
            result = execute_order(self.api_key, self.secret_key, order_details)
            
            if isinstance(result, dict) and "error" in result:
                logger.error(f"放置賣單失敗: {result['error']}")
            else:
                order_id = result.get('id')
                logger.info(f"成功放置賣單: 價格={next_price}, 數量={adjusted_quantity}, 訂單ID={order_id}")
                
                if order_id:
                    # 創建訂單信息
                    order_info = {
                        'order_id': order_id,
                        'side': 'Ask',
                        'quantity': adjusted_quantity,
                        'price': next_price,
                        'created_time': datetime.now(),
                        'created_from': f"BUY@{grid_price}"
                    }
                    
                    # 添加到按訂單ID索引的字典
                    self.grid_orders_by_id[order_id] = order_info
                    
                    # 添加到按價格索引的字典
                    if next_price not in self.grid_orders_by_price:
                        self.grid_orders_by_price[next_price] = []
                    self.grid_orders_by_price[next_price].append(order_info)
                    
                    # 添加到賣單字典
                    if next_price not in self.grid_sell_orders_by_price:
                        self.grid_sell_orders_by_price[next_price] = []
                    self.grid_sell_orders_by_price[next_price].append(order_info)
                    
                    # 更新舊的結構（向後兼容）
                    self.grid_orders[next_price] = {
                        'order_id': order_id,
                        'side': 'Ask',
                        'quantity': adjusted_quantity,
                        'price': next_price
                    }
                    self.grid_sell_orders[next_price] = self.grid_orders[next_price]
                    
                    # 更新訂單計數
                    self.orders_placed += 1
                    
                    # 記錄此價格點位的訂單數量
                    sell_count = len(self.grid_sell_orders_by_price.get(next_price, []))
                    logger.info(f"網格點位 {next_price} 現有賣單數: {sell_count}")
                    
                    # 更新網格狀態
                    self.grid_status[grid_price] = 'buy_filled'
                    self.grid_status[next_price] = 'sell_placed'
                    
                    # 建立依賴關係：當next_price的賣單成交後，才能在grid_price補充買單
                    self.grid_dependencies[grid_price] = next_price
                    logger.info(f"建立依賴關係: 價格點位 {grid_price} 依賴於 {next_price} 的賣單成交")
    
    def _place_buy_order_after_sell(self, executed_price, grid_price, quantity, actual_fee=None):
        """賣單成交後在下一個網格點位放置買單"""
        # 找到賣出價格的下一個更低網格點位
        next_price = None
        for price in sorted(self.grid_levels, reverse=True):
            if price < grid_price:
                next_price = price
                break
        
        if next_price:
            logger.info(f"在網格點位 {next_price} 放置買單 (賣出價格: {executed_price})")
            
            # 計算賣出實際獲得的資金
            if actual_fee is not None and isinstance(actual_fee, (int, float)):
                # 使用實際手續費（USDC為單位）
                if actual_fee > 0 and actual_fee < (executed_price * quantity):  # 合理的手續費範圍
                    sell_value = executed_price * quantity - actual_fee
                else:
                    sell_value = executed_price * quantity
            else:
                # 使用估計手續費
                sell_value = executed_price * quantity * 0.999
            
            # 計算可買入的數量
            buy_quantity = round_to_precision(sell_value / next_price, self.base_precision)
            
            # 檢查最小訂單大小
            if buy_quantity < self.min_order_size:
                logger.info(f"計算得出的買入數量 {buy_quantity} 低於最小訂單大小，使用最小值 {self.min_order_size}")
                buy_quantity = self.min_order_size
            
            # 檢查是否超過最大持倉限制
            net_position = self.total_bought - self.total_sold
            
            if net_position + buy_quantity > self.max_position:
                logger.warning(f"跳過買入：當前淨持倉 {net_position}，新增 {buy_quantity} 將超過最大限制 {self.max_position}")
                return
            
            # 放置買單
            order_details = {
                "orderType": "Limit",
                "price": str(next_price),
                "quantity": str(buy_quantity),
                "side": "Bid",
                "symbol": self.symbol,
                "timeInForce": "GTC",
                "postOnly": True
            }
            
            result = execute_order(self.api_key, self.secret_key, order_details)
            
            if isinstance(result, dict) and "error" in result:
                logger.error(f"放置買單失敗: {result['error']}")
            else:
                order_id = result.get('id')
                logger.info(f"成功放置買單: 價格={next_price}, 數量={buy_quantity}, 訂單ID={order_id}")
                
                if order_id:
                    # 創建訂單信息
                    order_info = {
                        'order_id': order_id,
                        'side': 'Bid',
                        'quantity': buy_quantity,
                        'price': next_price,
                        'created_time': datetime.now(),
                        'created_from': f"SELL@{grid_price}"
                    }
                    
                    # 添加到按訂單ID索引的字典
                    self.grid_orders_by_id[order_id] = order_info
                    
                    # 添加到按價格索引的字典
                    if next_price not in self.grid_orders_by_price:
                        self.grid_orders_by_price[next_price] = []
                    self.grid_orders_by_price[next_price].append(order_info)
                    
                    # 添加到買單字典
                    if next_price not in self.grid_buy_orders_by_price:
                        self.grid_buy_orders_by_price[next_price] = []
                    self.grid_buy_orders_by_price[next_price].append(order_info)
                    
                    # 更新舊的結構（向後兼容）
                    self.grid_orders[next_price] = {
                        'order_id': order_id,
                        'side': 'Bid',
                        'quantity': buy_quantity,
                        'price': next_price
                    }
                    self.grid_buy_orders[next_price] = self.grid_orders[next_price]
                    
                    # 更新訂單計數
                    self.orders_placed += 1
                    
                    # 記錄此價格點位的訂單數量
                    buy_count = len(self.grid_buy_orders_by_price.get(next_price, []))
                    logger.info(f"網格點位 {next_price} 現有買單數: {buy_count}")
                    
                    # 更新網格狀態
                    self.grid_status[grid_price] = 'sell_filled'
                    self.grid_status[next_price] = 'buy_placed'
                    
                    # 解除依賴關係：釋放依賴於此銷售點位的買點位
                    dependencies_resolved = []
                    for price, dependent_price in list(self.grid_dependencies.items()):
                        if dependent_price == grid_price:
                            # 移除依賴，表示可以在price處放置新的買單
                            del self.grid_dependencies[price]
                            dependencies_resolved.append(price)
                    
                    if dependencies_resolved:
                        logger.info(f"解除依賴關係: 價格點位 {dependencies_resolved} 已釋放")
    
    def _calculate_db_profit(self):
        """基於數據庫記錄計算已實現利潤（FIFO方法）"""
        try:
            # 獲取訂單歷史，注意這裡將返回一個列表
            order_history = self.db.get_order_history(self.symbol)
            if not order_history:
                return 0
            
            buy_trades = []
            sell_trades = []
            for side, quantity, price, maker, fee in order_history:
                if side == 'Bid':
                    buy_trades.append((float(price), float(quantity), float(fee)))
                elif side == 'Ask':
                    sell_trades.append((float(price), float(quantity), float(fee)))

            if not buy_trades or not sell_trades:
                return 0

            buy_queue = buy_trades.copy()
            total_profit = 0
            total_fees = 0

            for sell_price, sell_quantity, sell_fee in sell_trades:
                remaining_sell = sell_quantity
                total_fees += sell_fee

                while remaining_sell > 0 and buy_queue:
                    buy_price, buy_quantity, buy_fee = buy_queue[0]
                    matched_quantity = min(remaining_sell, buy_quantity)

                    trade_profit = (sell_price - buy_price) * matched_quantity
                    allocated_buy_fee = buy_fee * (matched_quantity / buy_quantity)
                    total_fees += allocated_buy_fee

                    net_trade_profit = trade_profit
                    total_profit += net_trade_profit

                    remaining_sell -= matched_quantity
                    if matched_quantity >= buy_quantity:
                        buy_queue.pop(0)
                    else:
                        remaining_fee = buy_fee * (1 - matched_quantity / buy_quantity)
                        buy_queue[0] = (buy_price, buy_quantity - matched_quantity, remaining_fee)

            self.total_fees = total_fees
            return total_profit

        except Exception as e:
            logger.error(f"計算數據庫利潤時出錯: {e}")
            import traceback
            traceback.print_exc()
            return 0
    
    def _update_trading_stats(self):
        """更新每日交易統計數據"""
        try:
            today = datetime.now().strftime('%Y-%m-%d')
            
            # 計算額外指標
            volatility = 0
            if self.ws and hasattr(self.ws, 'historical_prices'):
                volatility = calculate_volatility(self.ws.historical_prices)
            
            # 計算平均價差
            avg_spread = 0
            if self.ws and self.ws.bid_price and self.ws.ask_price:
                avg_spread = (self.ws.ask_price - self.ws.bid_price) / ((self.ws.ask_price + self.ws.bid_price) / 2) * 100
            
            # 準備統計數據
            stats_data = {
                'date': today,
                'symbol': self.symbol,
                'maker_buy_volume': self.maker_buy_volume,
                'maker_sell_volume': self.maker_sell_volume,
                'taker_buy_volume': self.taker_buy_volume,
                'taker_sell_volume': self.taker_sell_volume,
                'realized_profit': self.total_profit,
                'total_fees': self.total_fees,
                'net_profit': self.total_profit - self.total_fees,
                'avg_spread': avg_spread,
                'trade_count': self.trades_executed,
                'volatility': volatility
            }
            
            # 使用專門的函數來處理數據庫操作
            def safe_update_stats():
                try:
                    success = self.db.update_trading_stats(stats_data)
                    if not success:
                        logger.warning("更新交易統計失敗，下次再試")
                except Exception as db_err:
                    logger.error(f"更新交易統計時出錯: {db_err}")
            
            # 直接在當前線程執行，避免過多的並發操作
            safe_update_stats()
                
        except Exception as e:
            logger.error(f"更新交易統計數據時出錯: {e}")
            import traceback
            traceback.print_exc()
    
    def _calculate_average_buy_cost(self):
        """計算平均買入成本"""
        if not self.buy_trades:
            return 0
            
        total_buy_cost = sum(price * quantity for price, quantity in self.buy_trades)
        total_buy_quantity = sum(quantity for _, quantity in self.buy_trades)
        
        if not self.sell_trades or total_buy_quantity <= 0:
            return total_buy_cost / total_buy_quantity if total_buy_quantity > 0 else 0
        
        buy_queue = self.buy_trades.copy()
        consumed_cost = 0
        consumed_quantity = 0
        
        for _, sell_quantity in self.sell_trades:
            remaining_sell = sell_quantity
            
            while remaining_sell > 0 and buy_queue:
                buy_price, buy_quantity = buy_queue[0]
                matched_quantity = min(remaining_sell, buy_quantity)
                consumed_cost += buy_price * matched_quantity
                consumed_quantity += matched_quantity
                remaining_sell -= matched_quantity
                
                if matched_quantity >= buy_quantity:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_price, buy_quantity - matched_quantity)
        
        remaining_buy_quantity = total_buy_quantity - consumed_quantity
        remaining_buy_cost = total_buy_cost - consumed_cost
        
        if remaining_buy_quantity <= 0:
            if self.ws and self.ws.connected and self.ws.bid_price:
                return self.ws.bid_price
            return 0
        
        return remaining_buy_cost / remaining_buy_quantity
    
    def _calculate_session_profit(self):
        """計算本次執行的已實現利潤"""
        if not self.session_buy_trades or not self.session_sell_trades:
            return 0

        buy_queue = self.session_buy_trades.copy()
        total_profit = 0

        for sell_price, sell_quantity in self.session_sell_trades:
            remaining_sell = sell_quantity

            while remaining_sell > 0 and buy_queue:
                buy_price, buy_quantity = buy_queue[0]
                matched_quantity = min(remaining_sell, buy_quantity)

                # 計算這筆交易的利潤
                trade_profit = (sell_price - buy_price) * matched_quantity
                total_profit += trade_profit

                remaining_sell -= matched_quantity
                if matched_quantity >= buy_quantity:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_price, buy_quantity - matched_quantity)

        return total_profit

    def calculate_pnl(self):
        """計算已實現和未實現PnL"""
        # 總的已實現利潤
        realized_pnl = self._calculate_db_profit()
        
        # 本次執行的已實現利潤
        session_realized_pnl = self._calculate_session_profit()
        
        # 計算未實現利潤
        unrealized_pnl = 0
        net_position = self.total_bought - self.total_sold
        
        if net_position > 0:
            current_price = self.get_current_price()
            if current_price:
                avg_buy_cost = self._calculate_average_buy_cost()
                unrealized_pnl = (current_price - avg_buy_cost) * net_position
        
        # 返回總的PnL和本次執行的PnL
        return realized_pnl, unrealized_pnl, self.total_fees, realized_pnl - self.total_fees, session_realized_pnl, self.session_fees, session_realized_pnl - self.session_fees
    
    def get_current_price(self):
        """獲取當前價格（優先使用WebSocket數據）"""
        self.check_ws_connection()
        price = None
        if self.ws and self.ws.connected:
            price = self.ws.get_current_price()
        
        if price is None:
            ticker = get_ticker(self.symbol)
            if isinstance(ticker, dict) and "error" in ticker:
                logger.error(f"獲取價格失敗: {ticker['error']}")
                return None
            
            if "lastPrice" not in ticker:
                logger.error(f"獲取到的價格數據不完整: {ticker}")
                return None
            return float(ticker['lastPrice'])
        return price
    
    def get_market_depth(self):
        """獲取市場深度（優先使用WebSocket數據）"""
        self.check_ws_connection()
        bid_price, ask_price = None, None
        if self.ws and self.ws.connected:
            bid_price, ask_price = self.ws.get_bid_ask()
        
        if bid_price is None or ask_price is None:
            order_book = get_order_book(self.symbol)
            if isinstance(order_book, dict) and "error" in order_book:
                logger.error(f"獲取訂單簿失敗: {order_book['error']}")
                return None, None
            
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            if not bids or not asks:
                return None, None
            
            highest_bid = float(bids[-1][0]) if bids else None
            lowest_ask = float(asks[0][0]) if asks else None
            
            return highest_bid, lowest_ask
        
        return bid_price, ask_price
    
    def calculate_grid_levels(self):
        """計算網格價格點位"""
        current_price = self.get_current_price()
        if current_price is None:
            logger.error("無法獲取當前價格，無法計算網格")
            return []
        
        # 如果未設置網格上下限，則基於當前價格和價格範圍百分比自動計算
        if self.auto_price_range or (self.grid_upper_price is None or self.grid_lower_price is None):
            price_range_ratio = self.price_range_percent / 100
            self.grid_upper_price = round_to_tick_size(current_price * (1 + price_range_ratio), self.tick_size)
            self.grid_lower_price = round_to_tick_size(current_price * (1 - price_range_ratio), self.tick_size)
            logger.info(f"自動設置網格價格範圍: {self.grid_lower_price} - {self.grid_upper_price} (當前價格: {current_price})")
        
        # 計算網格間隔
        grid_interval = (self.grid_upper_price - self.grid_lower_price) / self.grid_num
        
        # 生成網格價格點位
        grid_levels = []
        for i in range(self.grid_num + 1):
            price = round_to_tick_size(self.grid_lower_price + i * grid_interval, self.tick_size)
            grid_levels.append(price)
        
        logger.info(f"計算得出 {len(grid_levels)} 個網格點位: {grid_levels[0]} - {grid_levels[-1]}")
        return grid_levels
    
    def subscribe_order_updates(self):
        """訂閲訂單更新流"""
        if not self.ws or not self.ws.is_connected():
            logger.warning("無法訂閲訂單更新：WebSocket連接不可用")
            return False
        
        # 嘗試訂閲訂單更新流
        stream = f"account.orderUpdate.{self.symbol}"
        if stream not in self.ws.subscriptions:
            retry_count = 0
            max_retries = 3
            success = False
            
            while retry_count < max_retries and not success:
                try:
                    success = self.ws.private_subscribe(stream)
                    if success:
                        logger.info(f"成功訂閲訂單更新: {stream}")
                        return True
                    else:
                        logger.warning(f"訂閲訂單更新失敗，嘗試重試... ({retry_count+1}/{max_retries})")
                except Exception as e:
                    logger.error(f"訂閲訂單更新時發生異常: {e}")
                
                retry_count += 1
                if retry_count < max_retries:
                    time.sleep(1)  # 重試前等待
            
            if not success:
                logger.error(f"在 {max_retries} 次嘗試後仍無法訂閲訂單更新")
                return False
        else:
            logger.info(f"已經訂閲了訂單更新: {stream}")
            return True
    
    def initialize_grid(self):
        """初始化網格交易"""
        logger.info("開始初始化網格交易...")
        
        # 計算網格價格點位
        self.grid_levels = self.calculate_grid_levels()
        if not self.grid_levels:
            logger.error("無法計算網格點位，初始化失敗")
            return False
        
        # 獲取當前價格
        current_price = self.get_current_price()
        if current_price is None:
            logger.error("無法獲取當前價格，初始化失敗")
            return False
        
        logger.info(f"當前價格: {current_price}")
        
        # 取消所有現有訂單
        self.cancel_existing_orders()
        
        # 獲取賬戶餘額
        balances = get_balance(self.api_key, self.secret_key)
        if isinstance(balances, dict) and "error" in balances:
            logger.error(f"獲取餘額失敗: {balances['error']}")
            return False
        
        base_balance = 0
        quote_balance = 0
        for asset, balance in balances.items():
            if asset == self.base_asset:
                base_balance = float(balance.get('available', 0))
            elif asset == self.quote_asset:
                quote_balance = float(balance.get('available', 0))
        
        logger.info(f"當前餘額: {base_balance} {self.base_asset}, {quote_balance} {self.quote_asset}")
        
        # 如果未設置訂單數量，根據當前餘額計算
        if self.order_quantity is None:
            # 估算合適的訂單數量
            total_quote_value = quote_balance + (base_balance * current_price)
            average_quantity = (total_quote_value / current_price) / (self.grid_num * 2)  # 平均分配到每個網格
            self.order_quantity = max(self.min_order_size, round_to_precision(average_quantity, self.base_precision))
            logger.info(f"自動計算訂單數量: {self.order_quantity} {self.base_asset}")
        
        # 初始化網格狀態和依賴關係
        self.grid_status = {}
        self.grid_dependencies = {}
        
        # 在網格點位下單
        placed_orders = 0
        
        # 清空訂單跟蹤結構
        self.grid_orders = {}
        self.grid_buy_orders = {}
        self.grid_sell_orders = {}
        self.grid_orders_by_price = {}
        self.grid_orders_by_id = {}
        self.grid_buy_orders_by_price = {}
        self.grid_sell_orders_by_price = {}
        
        # 設置買單和賣單
        for i, price in enumerate(self.grid_levels):
            # 根據價格相對於當前價格的位置決定買單或賣單
            if price < current_price:
                # 在當前價格下方設置買單
                order_details = {
                    "orderType": "Limit",
                    "price": str(price),
                    "quantity": str(self.order_quantity),
                    "side": "Bid",
                    "symbol": self.symbol,
                    "timeInForce": "GTC",
                    "postOnly": True
                }
                
                result = execute_order(self.api_key, self.secret_key, order_details)
                
                if isinstance(result, dict) and "error" in result:
                    logger.error(f"設置買單失敗 (價格 {price}): {result['error']}")
                else:
                    order_id = result.get('id')
                    logger.info(f"成功設置買單: 價格={price}, 數量={self.order_quantity}, 訂單ID={order_id}")
                    
                    # 更新舊訂單結構
                    self.grid_orders[price] = {
                        'order_id': order_id,
                        'side': 'Bid',
                        'quantity': self.order_quantity,
                        'price': price
                    }
                    self.grid_buy_orders[price] = self.grid_orders[price]
                    
                    # 更新新訂單結構
                    order_info = {
                        'order_id': order_id,
                        'side': 'Bid',
                        'quantity': self.order_quantity,
                        'price': price,
                        'created_time': datetime.now(),
                        'created_from': 'INIT'
                    }
                    
                    self.grid_orders_by_id[order_id] = order_info
                    
                    if price not in self.grid_orders_by_price:
                        self.grid_orders_by_price[price] = []
                    self.grid_orders_by_price[price].append(order_info)
                    
                    if price not in self.grid_buy_orders_by_price:
                        self.grid_buy_orders_by_price[price] = []
                    self.grid_buy_orders_by_price[price].append(order_info)
                    
                    # 設置網格狀態
                    self.grid_status[price] = 'buy_placed'
                    
                    placed_orders += 1
            
            elif price > current_price:
                # 在當前價格上方設置賣單
                # 確保有足夠的基礎資產
                if base_balance >= self.order_quantity:
                    order_details = {
                        "orderType": "Limit",
                        "price": str(price),
                        "quantity": str(self.order_quantity),
                        "side": "Ask",
                        "symbol": self.symbol,
                        "timeInForce": "GTC",
                        "postOnly": True
                    }
                    
                    result = execute_order(self.api_key, self.secret_key, order_details)
                    
                    if isinstance(result, dict) and "error" in result:
                        logger.error(f"設置賣單失敗 (價格 {price}): {result['error']}")
                    else:
                        order_id = result.get('id')
                        logger.info(f"成功設置賣單: 價格={price}, 數量={self.order_quantity}, 訂單ID={order_id}")
                        
                        # 更新舊訂單結構
                        self.grid_orders[price] = {
                            'order_id': order_id,
                            'side': 'Ask',
                            'quantity': self.order_quantity,
                            'price': price
                        }
                        self.grid_sell_orders[price] = self.grid_orders[price]
                        
                        # 更新新訂單結構
                        order_info = {
                            'order_id': order_id,
                            'side': 'Ask',
                            'quantity': self.order_quantity,
                            'price': price,
                            'created_time': datetime.now(),
                            'created_from': 'INIT'
                        }
                        
                        self.grid_orders_by_id[order_id] = order_info
                        
                        if price not in self.grid_orders_by_price:
                            self.grid_orders_by_price[price] = []
                        self.grid_orders_by_price[price].append(order_info)
                        
                        if price not in self.grid_sell_orders_by_price:
                            self.grid_sell_orders_by_price[price] = []
                        self.grid_sell_orders_by_price[price].append(order_info)
                        
                        # 設置網格狀態
                        self.grid_status[price] = 'sell_placed'
                        
                        placed_orders += 1
                        base_balance -= self.order_quantity  # 更新可用基礎資產餘額
                else:
                    logger.warning(f"基礎資產餘額不足，無法設置賣單 (價格 {price})")
        
        logger.info(f"網格初始化完成: 共放置 {placed_orders} 個訂單")
        self.grid_initialized = True
        self.orders_placed += placed_orders
        
        # 根據歷史交易初始化依賴關係
        self._initialize_dependencies_from_history()
        
        return True
    
    def place_limit_orders(self):
        """放置網格訂單"""
        # 如果網格尚未初始化，則先初始化
        if not self.grid_initialized:
            success = self.initialize_grid()
            if not success:
                logger.error("網格初始化失敗，無法放置訂單")
                return
            # 已在initialize_grid中放置訂單，無需繼續
            return
        
        # 網格已初始化，檢查並補充缺失的網格訂單
        current_price = self.get_current_price()
        if current_price is None:
            logger.error("無法獲取當前價格，無法檢查網格訂單")
            return
        
        # 獲取賬戶餘額
        balances = get_balance(self.api_key, self.secret_key)
        if isinstance(balances, dict) and "error" in balances:
            logger.error(f"獲取餘額失敗: {balances['error']}")
            return
        
        base_balance = 0
        quote_balance = 0
        for asset, balance in balances.items():
            if asset == self.base_asset:
                base_balance = float(balance.get('available', 0))
            elif asset == self.quote_asset:
                quote_balance = float(balance.get('available', 0))
        
        # 檢查網格點位
        buy_orders_per_level = {}
        sell_orders_per_level = {}
        
        # 統計每個價格點位的訂單數量
        for price in self.grid_levels:
            # 使用新的數據結構
            buy_orders_per_level[price] = len(self.grid_buy_orders_by_price.get(price, []))
            sell_orders_per_level[price] = len(self.grid_sell_orders_by_price.get(price, []))
        
        # 補充缺失的訂單
        orders_to_place = []
        
        for price in self.grid_levels:
            if price < current_price and buy_orders_per_level.get(price, 0) == 0:
                # 檢查此價格點位是否存在依賴關係 - 如果有依賴且依賴未解除，則不補單
                if price in self.grid_dependencies:
                    dependent_price = self.grid_dependencies[price]
                    logger.info(f"價格點位 {price} 的買單依賴於價位 {dependent_price} 的賣單成交，暫不補充")
                    continue
                
                # 此網格點位沒有買單，需要補充
                orders_to_place.append({
                    'price': price,
                    'side': 'Bid',
                    'quantity': self.order_quantity
                })
            elif price > current_price and sell_orders_per_level.get(price, 0) == 0:
                # 此網格點位沒有賣單，需要補充
                if base_balance >= self.order_quantity:
                    orders_to_place.append({
                        'price': price,
                        'side': 'Ask',
                        'quantity': self.order_quantity
                    })
                    base_balance -= self.order_quantity
                else:
                    logger.warning(f"基礎資產餘額不足，無法在價格 {price} 處補充賣單")
        
        # 放置新訂單
        orders_placed = 0
        for order_info in orders_to_place:
            order_details = {
                "orderType": "Limit",
                "price": str(order_info['price']),
                "quantity": str(order_info['quantity']),
                "side": order_info['side'],
                "symbol": self.symbol,
                "timeInForce": "GTC",
                "postOnly": True
            }
            
            result = execute_order(self.api_key, self.secret_key, order_details)
            
            if isinstance(result, dict) and "error" in result:
                logger.error(f"補充訂單失敗 (價格 {order_info['price']}, 方向 {order_info['side']}): {result['error']}")
            else:
                order_id = result.get('id')
                logger.info(f"成功補充訂單: 價格={order_info['price']}, 數量={order_info['quantity']}, 方向={order_info['side']}")
                
                price = order_info['price']
                side = order_info['side']
                quantity = order_info['quantity']
                
                # 更新舊訂單結構
                self.grid_orders[price] = {
                    'order_id': order_id,
                    'side': side,
                    'quantity': quantity,
                    'price': price
                }
                
                if side == 'Bid':
                    self.grid_buy_orders[price] = self.grid_orders[price]
                else:
                    self.grid_sell_orders[price] = self.grid_orders[price]
                
                # 更新新訂單結構
                new_order_info = {
                    'order_id': order_id,
                    'side': side,
                    'quantity': quantity,
                    'price': price,
                    'created_time': datetime.now(),
                    'created_from': 'REFILL'
                }
                
                self.grid_orders_by_id[order_id] = new_order_info
                
                if price not in self.grid_orders_by_price:
                    self.grid_orders_by_price[price] = []
                self.grid_orders_by_price[price].append(new_order_info)
                
                if side == 'Bid':
                    if price not in self.grid_buy_orders_by_price:
                        self.grid_buy_orders_by_price[price] = []
                    self.grid_buy_orders_by_price[price].append(new_order_info)
                    # 更新網格狀態
                    self.grid_status[price] = 'buy_placed'
                else:
                    if price not in self.grid_sell_orders_by_price:
                        self.grid_sell_orders_by_price[price] = []
                    self.grid_sell_orders_by_price[price].append(new_order_info)
                    # 更新網格狀態
                    self.grid_status[price] = 'sell_placed'
                
                orders_placed += 1
                self.orders_placed += 1
        
        if orders_placed > 0:
            logger.info(f"共補充了 {orders_placed} 個網格訂單")
    
    def cancel_existing_orders(self):
        """取消所有現有訂單"""
        open_orders = get_open_orders(self.api_key, self.secret_key, self.symbol)
        
        if isinstance(open_orders, dict) and "error" in open_orders:
            logger.error(f"獲取訂單失敗: {open_orders['error']}")
            return
        
        if not open_orders:
            logger.info("沒有需要取消的現有訂單")
            # 清空訂單跟蹤結構
            self.grid_orders = {}
            self.grid_buy_orders = {}
            self.grid_sell_orders = {}
            self.grid_orders_by_price = {}
            self.grid_orders_by_id = {}
            self.grid_buy_orders_by_price = {}
            self.grid_sell_orders_by_price = {}
            return
        
        logger.info(f"正在取消 {len(open_orders)} 個現有訂單")
        
        try:
            # 嘗試批量取消
            result = cancel_all_orders(self.api_key, self.secret_key, self.symbol)
            
            if isinstance(result, dict) and "error" in result:
                logger.error(f"批量取消訂單失敗: {result['error']}")
                logger.info("嘗試逐個取消...")
                
                # 初始化線程池
                with ThreadPoolExecutor(max_workers=5) as executor:
                    cancel_futures = []
                    
                    # 提交取消訂單任務
                    for order in open_orders:
                        order_id = order.get('id')
                        if not order_id:
                            continue
                        
                        future = executor.submit(
                            cancel_order, 
                            self.api_key, 
                            self.secret_key, 
                            order_id, 
                            self.symbol
                        )
                        cancel_futures.append((order_id, future))
                    
                    # 處理結果
                    for order_id, future in cancel_futures:
                        try:
                            res = future.result()
                            if isinstance(res, dict) and "error" in res:
                                logger.error(f"取消訂單 {order_id} 失敗: {res['error']}")
                            else:
                                logger.info(f"取消訂單 {order_id} 成功")
                                self.orders_cancelled += 1
                        except Exception as e:
                            logger.error(f"取消訂單 {order_id} 時出錯: {e}")
            else:
                logger.info("批量取消訂單成功")
                self.orders_cancelled += len(open_orders)
        except Exception as e:
            logger.error(f"取消訂單過程中發生錯誤: {str(e)}")
        
        # 等待一下確保訂單已取消
        time.sleep(1)
        
        # 檢查是否還有未取消的訂單
        remaining_orders = get_open_orders(self.api_key, self.secret_key, self.symbol)
        if remaining_orders and len(remaining_orders) > 0:
            logger.warning(f"警告: 仍有 {len(remaining_orders)} 個未取消的訂單")
        else:
            logger.info("所有訂單已成功取消")
        
        # 重置訂單跟蹤
        self.grid_orders = {}
        self.grid_buy_orders = {}
        self.grid_sell_orders = {}
        self.grid_orders_by_price = {}
        self.grid_orders_by_id = {}
        self.grid_buy_orders_by_price = {}
        self.grid_sell_orders_by_price = {}
        
        # 如果有網格初始化過，需要重新初始化網格
        self.grid_initialized = False
    
    def check_order_fills(self):
        """檢查訂單成交情況"""
        open_orders = get_open_orders(self.api_key, self.secret_key, self.symbol)
        
        if isinstance(open_orders, dict) and "error" in open_orders:
            logger.error(f"獲取訂單失敗: {open_orders['error']}")
            return
        
        # 獲取當前所有訂單ID
        current_order_ids = set()
        if open_orders:
            for order in open_orders:
                order_id = order.get('id')
                if order_id:
                    current_order_ids.add(order_id)
        
        # 檢查網格訂單成交情況
        filled_orders = []
        
        # 使用新訂單結構進行檢查
        for order_id, order_info in list(self.grid_orders_by_id.items()):
            if order_id not in current_order_ids:
                # 訂單不在當前訂單列表中，可能已成交
                grid_price = order_info.get('price')
                side = order_info.get('side')
                logger.info(f"網格點位 {grid_price} 的訂單可能已成交: {order_info}")
                filled_orders.append((order_id, order_info))
        
        # 從網格訂單中移除已成交的訂單
        for order_id, order_info in filled_orders:
            side = order_info.get('side')
            grid_price = order_info.get('price')
            self._remove_order(order_id, grid_price, side)
        
        # 記錄活躍訂單數量
        total_buy_orders = sum(len(orders) for orders in self.grid_buy_orders_by_price.values())
        total_sell_orders = sum(len(orders) for orders in self.grid_sell_orders_by_price.values())
        
        logger.info(f"當前活躍網格訂單: 買單 {total_buy_orders} 個, 賣單 {total_sell_orders} 個")
    
    def estimate_profit(self):
        """估算潛在利潤"""
        if not self.grid_levels or len(self.grid_levels) <= 1:
            logger.warning("未設置網格或網格點位不足，無法估算利潤")
            return
        
        # 計算網格間隔
        avg_grid_interval = (self.grid_levels[-1] - self.grid_levels[0]) / (len(self.grid_levels) - 1)
        
        # 計算每次成交的潛在利潤
        potential_profit_per_trade = avg_grid_interval * self.order_quantity
        
        # 估算平均每日成交次數（假設）- 這裡可以基於歷史數據優化
        estimated_daily_trades = 4  # 假設每天有4次網格成交
        
        # 計算總的PnL和本次執行的PnL
        realized_pnl, unrealized_pnl, total_fees, net_pnl, session_realized_pnl, session_fees, session_net_pnl = self.calculate_pnl()
        
        # 計算預估日利潤
        estimated_daily_profit = potential_profit_per_trade * estimated_daily_trades
        estimated_daily_fees = (self.total_fees / max(1, self.trades_executed)) * estimated_daily_trades
        estimated_net_daily_profit = estimated_daily_profit - estimated_daily_fees
        
        logger.info(f"\n--- 網格交易潛在利潤估算 ---")
        logger.info(f"網格數量: {len(self.grid_levels)}")
        logger.info(f"平均網格間隔: {avg_grid_interval:.8f} {self.quote_asset}")
        logger.info(f"每網格訂單數量: {self.order_quantity} {self.base_asset}")
        logger.info(f"每次成交潛在利潤: {potential_profit_per_trade:.8f} {self.quote_asset}")
        logger.info(f"估計每日成交次數: {estimated_daily_trades} 次")
        logger.info(f"估計日利潤: {estimated_daily_profit:.8f} {self.quote_asset}")
        logger.info(f"估計日手續費: {estimated_daily_fees:.8f} {self.quote_asset}")
        logger.info(f"估計日淨利潤: {estimated_net_daily_profit:.8f} {self.quote_asset}")
        
        logger.info(f"\n--- 當前交易統計 ---")
        logger.info(f"已實現利潤(總): {realized_pnl:.8f} {self.quote_asset}")
        logger.info(f"總手續費(總): {total_fees:.8f} {self.quote_asset}")
        logger.info(f"凈利潤(總): {net_pnl:.8f} {self.quote_asset}")
        logger.info(f"未實現利潤: {unrealized_pnl:.8f} {self.quote_asset}")
        
        # 打印本次執行的統計信息
        logger.info(f"\n---本次執行統計---")
        logger.info(f"本次執行已實現利潤: {session_realized_pnl:.8f} {self.quote_asset}")
        logger.info(f"本次執行手續費: {session_fees:.8f} {self.quote_asset}")
        logger.info(f"本次執行凈利潤: {session_net_pnl:.8f} {self.quote_asset}")
        
        session_buy_volume = sum(qty for _, qty in self.session_buy_trades)
        session_sell_volume = sum(qty for _, qty in self.session_sell_trades)
        
        logger.info(f"本次執行買入量: {session_buy_volume} {self.base_asset}, 賣出量: {session_sell_volume} {self.base_asset}")
        logger.info(f"本次執行Maker買入: {self.session_maker_buy_volume} {self.base_asset}, Maker賣出: {self.session_maker_sell_volume} {self.base_asset}")
        logger.info(f"本次執行Taker買入: {self.session_taker_buy_volume} {self.base_asset}, Taker賣出: {self.session_taker_sell_volume} {self.base_asset}")
    
    def print_trading_stats(self):
        """打印交易統計報表"""
        try:
            logger.info("\n=== 網格交易統計 ===")
            logger.info(f"交易對: {self.symbol}")
            
            today = datetime.now().strftime('%Y-%m-%d')
            
            # 獲取今天的統計數據
            today_stats = self.db.get_trading_stats(self.symbol, today)
            
            if today_stats and len(today_stats) > 0:
                stat = today_stats[0]
                maker_buy = stat['maker_buy_volume']
                maker_sell = stat['maker_sell_volume']
                taker_buy = stat['taker_buy_volume']
                taker_sell = stat['taker_sell_volume']
                profit = stat['realized_profit']
                fees = stat['total_fees']
                net = stat['net_profit']
                avg_spread = stat['avg_spread']
                volatility = stat['volatility']
                
                total_volume = maker_buy + maker_sell + taker_buy + taker_sell
                maker_percentage = ((maker_buy + maker_sell) / total_volume * 100) if total_volume > 0 else 0
                
                logger.info(f"\n今日統計 ({today}):")
                logger.info(f"買入量: {maker_buy + taker_buy} {self.base_asset}")
                logger.info(f"賣出量: {maker_sell + taker_sell} {self.base_asset}")
                logger.info(f"總成交量: {total_volume} {self.base_asset}")
                logger.info(f"Maker佔比: {maker_percentage:.2f}%")
                logger.info(f"波動率: {volatility:.4f}%")
                logger.info(f"毛利潤: {profit:.8f} {self.quote_asset}")
                logger.info(f"總手續費: {fees:.8f} {self.quote_asset}")
                logger.info(f"凈利潤: {net:.8f} {self.quote_asset}")
            
            # 網格狀態
            logger.info(f"\n網格狀態:")
            logger.info(f"網格數量: {len(self.grid_levels)}")
            logger.info(f"價格範圍: {self.grid_levels[0]} - {self.grid_levels[-1]} {self.quote_asset}")
            logger.info(f"每格訂單數量: {self.order_quantity} {self.base_asset}")
            
            # 使用新結構計算活躍訂單數
            total_buy_orders = sum(len(orders) for orders in self.grid_buy_orders_by_price.values())
            total_sell_orders = sum(len(orders) for orders in self.grid_sell_orders_by_price.values())
            
            logger.info(f"當前活躍網格訂單: 買單 {total_buy_orders} 個, 賣單 {total_sell_orders} 個")
            
            # 持倉統計
            net_position = self.total_bought - self.total_sold
            position_percentage = (net_position / self.max_position * 100) if self.max_position > 0 else 0
            logger.info(f"當前淨持倉: {net_position:.8f} {self.base_asset} ({position_percentage:.2f}% 占用)")
            
            # 獲取所有時間的總計
            all_time_stats = self.db.get_all_time_stats(self.symbol)
            
            if all_time_stats:
                total_maker_buy = all_time_stats['total_maker_buy']
                total_maker_sell = all_time_stats['total_maker_sell']
                total_taker_buy = all_time_stats['total_taker_buy']
                total_taker_sell = all_time_stats['total_taker_sell']
                total_profit = all_time_stats['total_profit']
                total_fees = all_time_stats['total_fees']
                total_net = all_time_stats['total_net_profit']
                
                total_volume = total_maker_buy + total_maker_sell + total_taker_buy + total_taker_sell
                maker_percentage = ((total_maker_buy + total_maker_sell) / total_volume * 100) if total_volume > 0 else 0
                
                logger.info(f"\n累計統計:")
                logger.info(f"買入量: {total_maker_buy + total_taker_buy} {self.base_asset}")
                logger.info(f"賣出量: {total_maker_sell + total_taker_sell} {self.base_asset}")
                logger.info(f"總成交量: {total_volume} {self.base_asset}")
                logger.info(f"Maker佔比: {maker_percentage:.2f}%")
                logger.info(f"毛利潤: {total_profit:.8f} {self.quote_asset}")
                logger.info(f"總手續費: {total_fees:.8f} {self.quote_asset}")
                logger.info(f"凈利潤: {total_net:.8f} {self.quote_asset}")
            
            # 查詢前10筆最新成交
            recent_trades = self.db.get_recent_trades(self.symbol, 10)
            
            if recent_trades and len(recent_trades) > 0:
                logger.info("\n最近10筆成交:")
                for i, trade in enumerate(recent_trades):
                    maker_str = "Maker" if trade['maker'] else "Taker"
                    logger.info(f"{i+1}. {trade['timestamp']} - {trade['side']} {trade['quantity']} @ {trade['price']} ({maker_str}) 手續費: {trade['fee']:.8f}")
            
            # 打印當前依賴關係
            if self.grid_dependencies:
                logger.info("\n當前網格依賴關係:")
                for price, dependent_price in self.grid_dependencies.items():
                    logger.info(f"價格點位 {price} 依賴於 {dependent_price} 的賣單成交")
        
        except Exception as e:
            logger.error(f"打印交易統計時出錯: {e}")
    
    def _ensure_data_streams(self):
        """確保所有必要的數據流訂閲都是活躍的"""
        # 檢查深度流訂閲
        if "depth" not in self.ws.subscriptions:
            logger.info("重新訂閲深度數據流...")
            self.ws.initialize_orderbook()  # 重新初始化訂單簿
            self.ws.subscribe_depth()
        
        # 檢查行情數據訂閲
        if "bookTicker" not in self.ws.subscriptions:
            logger.info("重新訂閲行情數據...")
            self.ws.subscribe_bookTicker()
        
        # 檢查私有訂單更新流
        if f"account.orderUpdate.{self.symbol}" not in self.ws.subscriptions:
            logger.info("重新訂閲私有訂單更新流...")
            self.subscribe_order_updates()
    
    def run(self, duration_seconds=3600, interval_seconds=60):
        """執行網格交易策略"""
        logger.info(f"開始運行網格交易策略: {self.symbol}")
        logger.info(f"運行時間: {duration_seconds} 秒, 間隔: {interval_seconds} 秒")
        logger.info(f"最大持倉限制: {self.max_position} {self.base_asset}")
        
        # 重置本次執行的統計數據
        self.session_start_time = datetime.now()
        self.session_buy_trades = []
        self.session_sell_trades = []
        self.session_fees = 0.0
        self.session_maker_buy_volume = 0.0
        self.session_maker_sell_volume = 0.0
        self.session_taker_buy_volume = 0.0
        self.session_taker_sell_volume = 0.0
        
        start_time = time.time()
        iteration = 0
        last_report_time = start_time
        report_interval = 300  # 5分鐘打印一次報表
        
        try:
            # 先確保 WebSocket 連接可用
            connection_status = self.check_ws_connection()
            if connection_status:
                # 初始化訂單簿和數據流
                if not self.ws.orderbook["bids"] and not self.ws.orderbook["asks"]:
                    self.ws.initialize_orderbook()
                
                # 檢查並確保所有數據流訂閲
                if "depth" not in self.ws.subscriptions:
                    self.ws.subscribe_depth()
                if "bookTicker" not in self.ws.subscriptions:
                    self.ws.subscribe_bookTicker()
                if f"account.orderUpdate.{self.symbol}" not in self.ws.subscriptions:
                    self.subscribe_order_updates()
            
            # 初始化網格交易
            if not self.grid_initialized:
                success = self.initialize_grid()
                if not success:
                    logger.error("網格初始化失敗，終止運行")
                    return
            
            while time.time() - start_time < duration_seconds:
                iteration += 1
                current_time = time.time()
                logger.info(f"\n=== 第 {iteration} 次迭代 ===")
                logger.info(f"時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                
                # 檢查連接並在必要時重連
                connection_status = self.check_ws_connection()
                
                # 如果連接成功，檢查並確保所有流訂閲
                if connection_status:
                    # 重新訂閲必要的數據流
                    self._ensure_data_streams()
                
                # 檢查訂單成交情況
                self.check_order_fills()
                
                # 補充網格訂單
                self.place_limit_orders()
                
                # 估算利潤
                self.estimate_profit()
                
                # 定期打印交易統計報表
                if current_time - last_report_time >= report_interval:
                    self.print_trading_stats()
                    last_report_time = current_time
                
                # 計算總的PnL和本次執行的PnL
                realized_pnl, unrealized_pnl, total_fees, net_pnl, session_realized_pnl, session_fees, session_net_pnl = self.calculate_pnl()
                
                # 計算持倉信息
                net_position = self.total_bought - self.total_sold
                position_percentage = (net_position / self.max_position * 100) if self.max_position > 0 else 0
                
                logger.info(f"\n統計信息:")
                logger.info(f"總交易次數: {self.trades_executed}")
                logger.info(f"總下單次數: {self.orders_placed}")
                logger.info(f"總取消訂單次數: {self.orders_cancelled}")
                logger.info(f"買入總量: {self.total_bought} {self.base_asset}")
                logger.info(f"賣出總量: {self.total_sold} {self.base_asset}")
                logger.info(f"淨持倉: {net_position:.8f} {self.base_asset} ({position_percentage:.2f}% 占用)")
                logger.info(f"總手續費: {total_fees:.8f} {self.quote_asset}")
                logger.info(f"已實現利潤: {realized_pnl:.8f} {self.quote_asset}")
                logger.info(f"凈利潤: {net_pnl:.8f} {self.quote_asset}")
                logger.info(f"未實現利潤: {unrealized_pnl:.8f} {self.quote_asset}")
                logger.info(f"WebSocket連接狀態: {'已連接' if self.ws and self.ws.is_connected() else '未連接'}")
                
                # 打印本次執行的統計數據
                logger.info(f"\n---本次執行統計---")
                session_buy_volume = sum(qty for _, qty in self.session_buy_trades)
                session_sell_volume = sum(qty for _, qty in self.session_sell_trades)
                logger.info(f"買入量: {session_buy_volume} {self.base_asset}, 賣出量: {session_sell_volume} {self.base_asset}")
                logger.info(f"Maker買入: {self.session_maker_buy_volume} {self.base_asset}, Maker賣出: {self.session_maker_sell_volume} {self.base_asset}")
                logger.info(f"Taker買入: {self.session_taker_buy_volume} {self.base_asset}, Taker賣出: {self.session_taker_sell_volume} {self.base_asset}")
                logger.info(f"本次執行已實現利潤: {session_realized_pnl:.8f} {self.quote_asset}")
                logger.info(f"本次執行手續費: {session_fees:.8f} {self.quote_asset}")
                logger.info(f"本次執行凈利潤: {session_net_pnl:.8f} {self.quote_asset}")
                
                # 使用新結構打印當前網格狀態
                total_buy_orders = sum(len(orders) for orders in self.grid_buy_orders_by_price.values())
                total_sell_orders = sum(len(orders) for orders in self.grid_sell_orders_by_price.values())
                
                # 打印當前網格狀態
                logger.info(f"\n---當前網格狀態---")
                logger.info(f"網格點位: {self.grid_levels[0]} - {self.grid_levels[-1]} ({len(self.grid_levels)} 個點位)")
                logger.info(f"活躍買單數量: {total_buy_orders}")
                logger.info(f"活躍賣單數量: {total_sell_orders}")
                logger.info(f"依賴關係數量: {len(self.grid_dependencies)}")
                current_price = self.get_current_price()
                if current_price:
                    logger.info(f"當前價格: {current_price} {self.quote_asset}")
                
                # 打印當前存在的依賴關係
                if self.grid_dependencies:
                    logger.info("\n當前網格依賴關係:")
                    for price, dependent_price in self.grid_dependencies.items():
                        logger.info(f"  價格點位 {price} 依賴於 {dependent_price} 的賣單成交")
                
                wait_time = interval_seconds
                logger.info(f"等待 {wait_time} 秒後進行下一次迭代...")
                time.sleep(wait_time)
                
            # 結束運行時打印最終報表
            logger.info("\n=== 網格交易策略運行結束 ===")
            self.print_trading_stats()
            
        except KeyboardInterrupt:
            logger.info("\n用户中斷，停止網格交易")
            
        finally:
            logger.info("取消所有網格訂單...")
            self.cancel_existing_orders()
            
            # 關閉 WebSocket
            if self.ws:
                self.ws.close()
            
            # 關閉數據庫連接
            if self.db:
                self.db.close()
                logger.info("數據庫連接已關閉")