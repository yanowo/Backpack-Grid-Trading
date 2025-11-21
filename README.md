# 注意：此代碼庫已不再更新
> 此庫已整合到 [Backpack-MM-Simple](https://github.com/yanowo/Backpack-MM-Simple/tree/main) ，請前往該倉庫查看最新代碼與後續更新。

> This repository has been merged into [Backpack-MM-Simple](https://github.com/yanowo/Backpack-MM-Simple/tree/main) Please visit that repository for the latest code and future updates.
# Backpack Exchange 網格交易程序

這是一個針對 Backpack Exchange 的加密貨幣網格交易程序。該程序提供自動化網格交易功能，通過在預設價格範圍內設置多個買賣網格點，實現低買高賣賺取利潤。

Backpack 註冊連結：[https://backpack.exchange/refer/yan](https://backpack.exchange/refer/yan)

Twitter：[Yan Practice ⭕散修](https://x.com/practice_y11)

## 功能特點

- 自動化網格交易策略
- 自動或手動設置價格範圍
- 可調節網格數量
- 買單成交後自動補充賣單
- 賣單成交後自動補充買單
- 詳細的交易統計
- WebSocket 實時數據連接
- 命令行界面

## 項目結構

```
Backpack-Grid-Trading/
│
├── api/                  # API相關模塊
│   ├── __init__.py
│   ├── auth.py           # API認證和簽名相關
│   └── client.py         # API請求客戶端
│
├── websocket/            # WebSocket模塊
│   ├── __init__.py
│   └── client.py         # WebSocket客戶端
│
├── database/             # 數據庫模塊
│   ├── __init__.py
│   └── db.py             # 數據庫操作
│
├── strategies/           # 策略模塊
│   ├── __init__.py
│   └── grid_trader.py    # 網格交易策略
│
├── utils/                # 工具模塊
│   ├── __init__.py
│   └── helpers.py        # 輔助函數
│   └── grid_helper.py    # 輔助設定
│
├── cli/                  # 命令行界面
│   ├── __init__.py
│   └── commands.py       # 命令行命令
│
├── config.py             # 配置文件
├── logger.py             # 日誌配置
├── main.py               # 主執行文件
├── run.py                # 統一入口文件
└── requirements.txt      # 依賴元件
└── README.md             # 說明文檔
```

## 環境要求

- Python 3.8 或更高版本
- 所需第三方庫：
  - PyNaCl (用於API簽名)
  - requests
  - websocket-client
  - numpy
  - python-dotenv

## 安裝

1. 克隆或下載此代碼庫:

```bash
git clone https://github.com/yanowo/Backpack-MM-Simple.git
cd Backpack-MM-Simple
```

2. 安裝依賴:

```bash
pip install -r requirements.txt
```

3. 設置環境變數:

創建 `.env` 文件並添加:

```
API_KEY=your_api_key
SECRET_KEY=your_secret_key
PROXY_WEBSOCKET=http://user:pass@host:port/ 或者 http://host:port (若不需要則留空或移除)
```

## 使用方法

### 統一入口

```bash
# 啟動命令行界面 (默認)
python run.py
```

### 命令行界面

啟動命令行界面:

```bash
python main.py --cli
```

### 直接執行網格交易策略

```bash
python main.py --symbol SOL_USDC --grid-upper 30.5 --grid-lower 29.5 --grid-num 10 --duration 3600 --interval 60
```

### 命令行參數

- `--api-key`: API 密鑰 (可選，默認使用環境變數)
- `--secret-key`: API 密鑰 (可選，默認使用環境變數)
- `--ws-proxy`: Websocket 代理 (可選，默認使用環境變數)

### 網格交易參數

- `--symbol`: 交易對 (例如: SOL_USDC)
- `--grid-upper`: 網格上限價格
- `--grid-lower`: 網格下限價格
- `--grid-num`: 網格數量 (默認: 10)
- `--quantity`: 每個網格的訂單數量 (可選)
- `--auto-price`: 自動設置價格範圍 (基於當前市場價格)
- `--price-range`: 自動模式下的價格範圍百分比 (默認: 5.0)
- `--duration`: 運行時間（秒）(默認: 3600)
- `--interval`: 更新間隔（秒）(默認: 60)

## 運行示例

### 自動設置價格範圍

```bash
python run.py --symbol SOL_USDC --auto-price --price-range 5.0 --grid-num 10
```

### 手動設置價格範圍

```bash
python run.py --symbol SOL_USDC --grid-upper 30.5 --grid-lower 29.5 --grid-num 15
```

### 指定訂單數量

```bash
python run.py --symbol SOL_USDC --auto-price --price-range 3.0 --grid-num 8 --quantity 0.5
```

### 長時間運行示例

```bash
python run.py --symbol SOL_USDC --auto-price --price-range 4.0 --duration 86400 --interval 120
```

### 完整參數示例

```bash
python run.py --symbol SOL_USDC --grid-upper 30.5 --grid-lower 29.5 --grid-num 12 --quantity 0.2 --duration 7200 --interval 60
``` 

## 策略說明

網格交易是一種常見的量化交易策略，特別適合在震盪行情中使用。策略邏輯如下：

1. 在設定的價格區間內（上限和下限之間）均勻劃分多個價格點
2. 在每個價格點上放置買單或賣單
3. 當買單成交後，自動在更高的價格點放置賣單
4. 當賣單成交後，自動在更低的價格點放置買單
5. 通過低買高賣的差價獲取利潤

該策略在震盪市場中效果較好，但在單邊行情中可能面臨風險。建議根據市場情況調整網格參數。

## 注意事項

- 交易涉及風險，請謹慎使用
- 建議先在小資金上測試策略效果
- 定期檢查交易統計以評估策略表現
- 網格交易需要足夠的資金以覆蓋整個網格區間
- 強烈建議在相對穩定的市場使用網格交易