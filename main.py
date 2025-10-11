from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import sys
import os
from dotenv import load_dotenv
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup as bs
import asyncio
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Try to import Option Trading System
try:
    from main import OptimizedOptionTradingSystem as OptionTradingSystem
    TRADING_SYSTEM_AVAILABLE = True
    logger.info("✅ Real trading system imported successfully")
except ImportError as e:
    logger.warning(f"⚠️ Could not import main trading system: {e}")
    logger.info("📄 Using dummy trading system for testing")
    
    # Dummy trading system for testing
    class OptionTradingSystem:
        def __init__(self):
            self.is_running = False
        
        async def initialize_system_fast(self):
            logger.info("🤖 Initializing dummy trading system")
            await asyncio.sleep(1)
            self.is_running = True
            return True
        
        def get_dropdown_options(self):
            return {
                "symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"],
                "option_expiry": ["07Dec2023", "14Dec2023", "21Dec2023", "28Dec2023"],
                "future_expiry": ["07Dec2023", "14Dec2023", "21Dec2023", "28Dec2023"]
            }
        
        async def fetch_option_data_fast(self, symbol, option_expiry, future_expiry, chain_length):
            logger.info(f"📊 Generating dummy data for {symbol}")
            await asyncio.sleep(0.5)
            
            base_prices = {
                'NIFTY': 19500, 
                'BANKNIFTY': 45000, 
                'FINNIFTY': 19800, 
                'MIDCPNIFTY': 9500,
                'SENSEX': 65000
            }
            
            base_price = base_prices.get(symbol, 19500)
            strike_diff = 50 if symbol in ['NIFTY', 'FINNIFTY'] else 100
            
            option_chain = []
            start_strike = base_price - (chain_length // 2 * strike_diff)
            
            for i in range(chain_length):
                strike = start_strike + (i * strike_diff)
                
                if strike <= base_price:
                    call_ltp = max(base_price - strike + (50 - abs(strike - base_price) * 0.1), 0.05)
                    put_ltp = max(10 + abs(strike - base_price) * 0.05, 0.05)
                else:
                    call_ltp = max(50 - (strike - base_price) * 0.1, 0.05)
                    put_ltp = max(strike - base_price + (10 + abs(strike - base_price) * 0.05), 0.05)
                
                option_chain.append({
                    'strike': strike,
                    'call_ltp': round(call_ltp, 2),
                    'call_volume': 1000 + (i * 500),
                    'call_oi': 50000 + (i * 10000),
                    'put_ltp': round(put_ltp, 2),
                    'put_volume': 800 + (i * 300),
                    'put_oi': 40000 + (i * 8000)
                })
            
            return {
                'symbol': symbol,
                'option_expiry': option_expiry,
                'future_expiry': future_expiry,
                'chain_length': chain_length,
                'underlying_price': float(base_price),
                'option_chain': option_chain,
                'timestamp': datetime.now().isoformat(),
                'data_source': 'dummy_system'
            }
        
        async def cleanup(self):
            logger.info("🧹 Dummy system cleanup")
            self.is_running = False
    
    TRADING_SYSTEM_AVAILABLE = False

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"

if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
    print("✅ Loaded environment variables from .env file")
else:
    print("ℹ️  Using system environment variables")

# FastAPI initialization
app = FastAPI(
    title="Unified Trading API - Option Trading & Chartink",
    description="Combined API for Option Trading System and Chartink Scanner",
    version="2.0.0"
)

# CORS middleware
# Allow all origins while supporting credentials by reflecting the request Origin
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== MODELS ====================
class ScanCondition(BaseModel):
    scan_clause: str
    
    class Config:
        json_schema_extra = {
            "example": {
                "scan_clause": "( {cash} ( daily max( 5 , daily close ) > 6 days ago max( 120 , daily close ) * 1.05 and daily volume > daily sma( volume,5 ) and daily close > 1 day ago close ) )"
            }
        }

class StockData(BaseModel):
    data: List[Dict[str, Any]]
    total_count: int
    timestamp: str

class TradingRequest(BaseModel):
    symbol: str = Field(..., description="Trading symbol (NIFTY, BANKNIFTY, etc.)")
    option_expiry: str = Field(..., description="Option expiry date")
    future_expiry: str = Field(..., description="Future expiry date")
    chain_length: int = Field(20, ge=5, le=50, description="Option chain length")

class IndexSelectionRequest(BaseModel):
    index: str = Field(..., description="Index name")
    expiry_date: Optional[str] = Field(None, description="Specific expiry date")
    strike_range: Optional[int] = Field(20, description="Number of strikes to fetch")

# Global variables for Option Trading System
trading_system: Optional[OptionTradingSystem] = None
system_status = {
    "initialized": False,
    "last_updated": None,
    "error_message": None,
    "auto_initialized": False
}
init_lock = asyncio.Lock()

# Chartink Scraper Class
class ChartinkScraper:
    """Scraper class for Chartink"""
    
    def __init__(self):
        self.base_url = "https://chartink.com/screener/process"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
    
    def get_csrf_token(self) -> str:
        """Fetch CSRF token from Chartink"""
        try:
            response = self.session.get(self.base_url, timeout=10)
            response.raise_for_status()
            
            soup = bs(response.content, "html.parser")
            meta = soup.find("meta", {"name": "csrf-token"})
            
            if not meta:
                raise ValueError("CSRF token not found")
            
            return meta["content"]
        except Exception as e:
            print(f"❌ Error fetching CSRF token: {e}")
            raise
    
    def fetch_stocks(self, scan_clause: str) -> Dict[str, Any]:
        """Fetch stock data based on scan conditions"""
        try:
            csrf_token = self.get_csrf_token()
            headers = {"x-csrf-token": csrf_token}
            condition = {"scan_clause": scan_clause}
            
            response = self.session.post(
                self.base_url,
                headers=headers,
                data=condition,
                timeout=15
            )
            response.raise_for_status()
            
            data = response.json()
            return data
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Request error: {e}")
            raise
        except Exception as e:
            print(f"❌ Error fetching stocks: {e}")
            raise

# Initialize Chartink scraper
chartink_scraper = ChartinkScraper()

# ==================== OPTION TRADING FUNCTIONS ====================
def get_available_indices() -> Dict[str, Dict]:
    return {
        "NIFTY": {
            "name": "NIFTY 50",
            "symbol": "NIFTY",
            "lot_size": 50,
            "strike_difference": 50
        },
        "BANKNIFTY": {
            "name": "BANK NIFTY",
            "symbol": "BANKNIFTY",
            "lot_size": 15,
            "strike_difference": 100
        },
        "FINNIFTY": {
            "name": "FIN NIFTY",
            "symbol": "FINNIFTY",
            "lot_size": 40,
            "strike_difference": 50
        },
        "MIDCAP": {
            "name": "NIFTY MIDCAP SELECT",
            "symbol": "MIDCPNIFTY",
            "lot_size": 75,
            "strike_difference": 25
        },
        "SENSEX": {
            "name": "SENSEX",
            "symbol": "SENSEX",
            "lot_size": 10,
            "strike_difference": 100
        }
    }

def generate_expiry_dates(months: int = 3) -> List[str]:
    """Generate realistic expiry dates (Thursdays)"""
    dates = []
    current_date = datetime.now()
    
    for week in range(months * 4):
        days_ahead = (3 - current_date.weekday()) % 7
        if days_ahead <= 0:
            days_ahead += 7
        
        thursday = current_date + timedelta(days=days_ahead)
        dates.append(thursday.strftime("%d%b%Y"))
        current_date = thursday + timedelta(days=7)
    
    return dates

def is_market_hours() -> bool:
    """Check if market is currently open"""
    now = datetime.now().time()
    market_start = datetime.strptime("09:15", "%H:%M").time()
    market_end = datetime.strptime("15:30", "%H:%M").time()
    return market_start <= now <= market_end

async def ensure_system_initialized():
    """Ensure system is initialized before processing requests"""
    global trading_system, system_status
    
    async with init_lock:
        if system_status["initialized"]:
            return True
        
        try:
            logger.info("🔄 Auto-initializing option trading system...")
            
            if not trading_system:
                trading_system = OptionTradingSystem()
            
            success = await trading_system.initialize_system_fast()
            
            if success:
                system_status["initialized"] = True
                system_status["last_updated"] = datetime.now().isoformat()
                system_status["error_message"] = None
                system_status["auto_initialized"] = True
                
                logger.info("✅ Auto-initialization successful!")
                return True
            else:
                raise Exception("System initialization returned False")
                
        except Exception as e:
            error_msg = f"Auto-initialization failed: {str(e)}"
            system_status["error_message"] = error_msg
            logger.error(f"❌ {error_msg}")
            return False

# ==================== API ENDPOINTS ====================

@app.get("/", tags=["System"])
async def health_check():
    return {
        "status": "healthy",
        "service": "Unified Trading API - Option Trading & Chartink",
        "version": "2.0.0",
        "timestamp": str(datetime.now()),
        "option_trading_system": {
            "available": TRADING_SYSTEM_AVAILABLE,
            "initialized": system_status["initialized"],
            "auto_initialize": True
        },
        "chartink_scanner": "enabled",
        "endpoints": {
            "option_trading_docs": "/docs#/Option%20Trading",
            "chartink_docs": "/docs#/Chartink"
        }
    }

# ==================== CHARTINK ENDPOINTS ====================

@app.post("/scan", response_model=StockData, tags=["Chartink"])
async def scan_stocks(condition: ScanCondition):
    """
    Scan stocks with Chartink custom conditions
    
    - **scan_clause**: Chartink scan condition query
    """
    try:
        print(f"📊 Scanning with condition: {condition.scan_clause}")
        
        result = chartink_scraper.fetch_stocks(condition.scan_clause)
        
        if not result or "data" not in result:
            raise HTTPException(status_code=404, detail="No data found")
        
        return {
            "data": result["data"],
            "total_count": len(result["data"]),
            "timestamp": datetime.now().isoformat()
        }
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")
        raise HTTPException(status_code=503, detail="Failed to fetch data from Chartink")
    except Exception as e:
        print(f"❌ Scan failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================== OPTION TRADING ENDPOINTS ====================

@app.post("/initialize", tags=["Option Trading"])
async def initialize_system():
    """Initialize the option trading system (manual trigger - not required with auto-init)"""
    result = await ensure_system_initialized()
    
    if result:
        return {
            "message": "✅ System initialized successfully",
            "status": "ready",
            "initialized_at": system_status["last_updated"],
            "system_type": "real" if TRADING_SYSTEM_AVAILABLE else "demo",
            "auto_initialized": system_status["auto_initialized"]
        }
    else:
        raise HTTPException(
            status_code=500, 
            detail=system_status.get("error_message", "Initialization failed")
        )

@app.get("/status", tags=["Option Trading"])
async def get_system_status():
    """Get comprehensive option trading system status"""
    return {
        "system_initialized": system_status["initialized"],
        "auto_initialized": system_status.get("auto_initialized", False),
        "last_updated": system_status["last_updated"],
        "error_message": system_status["error_message"],
        "current_time": datetime.now().isoformat(),
        "market_hours": is_market_hours(),
        "trading_system_available": TRADING_SYSTEM_AVAILABLE,
        "trading_system_running": trading_system.is_running if trading_system else False
    }

@app.get("/indices", tags=["Option Trading"])
async def get_indices():
    """Get available indices"""
    indices = get_available_indices()
    return {
        "indices": indices,
        "count": len(indices),
        "default": "NIFTY"
    }

@app.get("/expiry-dates", tags=["Option Trading"])
async def get_expiry_dates(
    symbol: str = Query("NIFTY", description="Symbol"),
    months: int = Query(3, ge=1, le=12, description="Months ahead")
):
    """Get expiry dates for a symbol"""
    await ensure_system_initialized()
    
    try:
        if trading_system and system_status["initialized"]:
            dropdown_options = trading_system.get_dropdown_options()
            return {
                "symbol": symbol,
                "expiry_dates": dropdown_options.get("option_expiry", []),
                "future_expiry_dates": dropdown_options.get("future_expiry", []),
                "source": "trading_system"
            }
        
        expiry_dates = generate_expiry_dates(months=months)
        return {
            "symbol": symbol,
            "expiry_dates": expiry_dates,
            "future_expiry_dates": expiry_dates,
            "source": "generated"
        }
        
    except Exception as e:
        logger.error(f"Error getting expiry dates: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/fetch-option-data", tags=["Option Trading"])
async def fetch_option_data(request: TradingRequest):
    """Fetch option chain data (auto-initializes if needed)"""
    initialized = await ensure_system_initialized()
    
    if not initialized:
        raise HTTPException(
            status_code=500,
            detail=f"❌ Auto-initialization failed: {system_status.get('error_message', 'Unknown error')}"
        )
    
    try:
        logger.info(f"📊 Fetching option data for {request.symbol}")
        
        data = await trading_system.fetch_option_data_fast(
            symbol=request.symbol,
            option_expiry=request.option_expiry,
            future_expiry=request.future_expiry,
            chain_length=request.chain_length
        )
        
        if data:
            return {
                "status": "success",
                "timestamp": datetime.now().isoformat(),
                "request": request.dict(),
                "data": data,
                "auto_initialized": system_status.get("auto_initialized", False)
            }
        else:
            raise HTTPException(status_code=500, detail="No data returned")
            
    except Exception as e:
        error_msg = f"Failed to fetch option data: {str(e)}"
        logger.error(f"❌ {error_msg}")
        raise HTTPException(status_code=500, detail=error_msg)

@app.post("/index-selection", tags=["Option Trading"])
async def select_index(request: IndexSelectionRequest):
    """Select index and get its option data (auto-initializes if needed)"""
    initialized = await ensure_system_initialized()
    
    if not initialized:
        raise HTTPException(
            status_code=500,
            detail=f"❌ Auto-initialization failed: {system_status.get('error_message', 'Unknown error')}"
        )
    
    try:
        indices = get_available_indices()
        index_key = request.index.upper()
        
        if index_key not in indices:
            available = list(indices.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Invalid index '{request.index}'. Available: {available}"
            )
        
        index_info = indices[index_key]
        symbol = index_info["symbol"]
        
        if request.expiry_date:
            expiry_date = request.expiry_date
        else:
            dropdown_options = trading_system.get_dropdown_options()
            available_expiries = dropdown_options.get("option_expiry", [])
            expiry_date = available_expiries[0] if available_expiries else generate_expiry_dates(months=1)[0]
        
        data = await trading_system.fetch_option_data_fast(
            symbol=symbol,
            option_expiry=expiry_date,
            future_expiry=expiry_date,
            chain_length=request.strike_range or 20
        )
        
        return {
            "status": "success",
            "selected_index": {
                "key": index_key,
                "info": index_info,
                "symbol": symbol
            },
            "expiry_date": expiry_date,
            "strike_range": request.strike_range,
            "data": data,
            "timestamp": datetime.now().isoformat(),
            "auto_initialized": system_status.get("auto_initialized", False)
        }
        
    except Exception as e:
        error_msg = f"Index selection failed: {str(e)}"
        logger.error(f"❌ {error_msg}")
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/market-status", tags=["Option Trading"])
async def get_market_status():
    """Get current market status"""
    now = datetime.now()
    market_open = is_market_hours()
    
    return {
        "is_open": market_open,
        "current_time": now.isoformat(),
        "market_hours": {
            "start": "09:15",
            "end": "15:30"
        },
        "status": "OPEN" if market_open else "CLOSED"
    }

@app.get("/health", tags=["System"])
async def health_check_endpoint():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0.0",
        "auto_initialize": True
    }

# ==================== STARTUP/SHUTDOWN EVENTS ====================

@app.on_event("startup")
async def startup_event():
    logger.info("🚀 Unified Trading API Server starting up...")
    logger.info(f"📊 Option Trading System Available: {TRADING_SYSTEM_AVAILABLE}")
    logger.info("📊 Chartink Scanner: ✅ Active")
    logger.info("🔄 Auto-initialization enabled for Option Trading System")
    logger.info("✅ Server ready to accept connections")

@app.on_event("shutdown")
async def shutdown_event():
    global trading_system
    logger.info("🛑 Server shutting down...")
    if trading_system:
        try:
            await trading_system.cleanup()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
    logger.info("👋 Goodbye!")

# ==================== MAIN ====================

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.environ.get("PORT", 8000))
    logger.info("🌟 Starting Unified Trading API Server")
    logger.info(f"📚 API Documentation: http://localhost:{port}/docs")
    logger.info("🔄 Option Trading System: Auto-initialize on first request")
    logger.info("📊 Chartink Scanner: Ready")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )