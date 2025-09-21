from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sys
import os
import json
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, time, timedelta
import threading
import schedule
import time as time_module
from typing import List, Literal
import pyotp

# Import SmartAPI
try:
    from SmartApi import SmartConnect
except ImportError:
    print("❌ ERROR: smartapi-python not installed!")
    print("Please run: pip install smartapi-python")
    sys.exit(1)

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
TOKEN_FILE = BASE_DIR / "angel_tokens.json"

if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
    print("✅ Loaded environment variables from .env file")
else:
    print("ℹ️  Using system environment variables")

# FastAPI initialization
app = FastAPI(
    title="Angel One Stock Data API",
    description="API for OHLC data and market movers using Angel One SmartAPI with persistent tokens",
    version="2.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic Models
class OHLCRequest(BaseModel):
    exchange: str = "NSE"
    tradingSymbol: str
    symbolToken: str

class OHLCBatchRequest(BaseModel):
    symbols: List[dict]

class MarketMoversRequest(BaseModel):
    datatype: Literal["PercOIGainers", "PercOILosers", "PercPriceGainers", "PercPriceLosers"]
    expirytype: Literal["NEAR", "NEXT", "FAR"] = "NEAR"

# Angel One API credentials
api_key = os.getenv("ANGEL_API_KEY", "").strip()
client_id = os.getenv("ANGEL_CLIENT_ID", "").strip()
password = os.getenv("ANGEL_PASSWORD", "").strip()
totp_token = os.getenv("ANGEL_TOTP_TOKEN", "").strip()

# Global variables
smart_api = None
auth_token = None
refresh_token = None
feed_token = None
last_token_refresh = None

def save_tokens_to_file():
    """Save tokens to file for persistence"""
    try:
        token_data = {
            "refresh_token": refresh_token,
            "auth_token": auth_token,
            "feed_token": feed_token,
            "last_refresh": last_token_refresh.isoformat() if last_token_refresh else None,
            "client_id": client_id
        }
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f, indent=2)
        print(f"💾 Tokens saved to {TOKEN_FILE}")
    except Exception as e:
        print(f"⚠️  Error saving tokens: {e}")

def load_tokens_from_file():
    """Load tokens from file"""
    global refresh_token, auth_token, feed_token, last_token_refresh
    
    try:
        if TOKEN_FILE.exists():
            with open(TOKEN_FILE, 'r') as f:
                token_data = json.load(f)
            
            if token_data.get('client_id') == client_id:
                refresh_token = token_data.get('refresh_token')
                auth_token = token_data.get('auth_token')
                feed_token = token_data.get('feed_token')
                
                last_refresh_str = token_data.get('last_refresh')
                if last_refresh_str:
                    last_token_refresh = datetime.fromisoformat(last_refresh_str)
                
                print(f"📂 Loaded tokens from file (last refresh: {last_token_refresh})")
                return True
            else:
                print("⚠️  Token file is for different client, ignoring")
                return False
        return False
    except Exception as e:
        print(f"⚠️  Error loading tokens: {e}")
        return False

def generate_totp():
    """Generate TOTP for 2FA"""
    try:
        totp = pyotp.TOTP(totp_token)
        return totp.now()
    except Exception as e:
        print(f"❌ Error generating TOTP: {e}")
        raise HTTPException(status_code=500, detail=f"TOTP generation failed: {str(e)}")

def full_login():
    """Perform full TOTP login (only when refresh token expires)"""
    global smart_api, auth_token, refresh_token, feed_token, last_token_refresh
    
    try:
        if not all([api_key, client_id, password, totp_token]):
            raise HTTPException(
                status_code=500,
                detail="Missing Angel One credentials. Check environment variables."
            )
        
        print(f"🔐 [{datetime.now()}] Performing FULL LOGIN with TOTP...")
        
        smart_api = SmartConnect(api_key=api_key)
        totp_code = generate_totp()
        data = smart_api.generateSession(client_id, password, totp_code)
        
        if data['status']:
            auth_token = data['data']['jwtToken']
            refresh_token = data['data']['refreshToken']
            feed_token = smart_api.getfeedToken()
            last_token_refresh = datetime.now()
            
            save_tokens_to_file()
            
            print(f"✅ FULL LOGIN successful! Refresh token valid for ~30 days")
            print(f"   Auth Token: {auth_token[:20]}...")
            print(f"   Refresh Token: {refresh_token[:20]}...")
            return True
        else:
            raise HTTPException(status_code=500, detail=f"Login failed: {data.get('message', 'Unknown error')}")
            
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error during full login: {e}")
        raise HTTPException(status_code=500, detail=f"Full login failed: {str(e)}")

def refresh_session():
    """Refresh session using refresh token (no TOTP needed!)"""
    global smart_api, auth_token, feed_token, last_token_refresh
    
    try:
        if not refresh_token:
            print("⚠️  No refresh token available, performing full login...")
            return full_login()
        
        print(f"🔄 [{datetime.now()}] Refreshing session using refresh token...")
        
        if not smart_api:
            smart_api = SmartConnect(api_key=api_key)
        
        data = smart_api.generateToken(refresh_token)
        
        if data['status']:
            auth_token = data['data']['jwtToken']
            feed_token = data['data']['feedToken']
            last_token_refresh = datetime.now()
            
            save_tokens_to_file()
            
            print(f"✅ Session refreshed successfully! (No TOTP needed)")
            return True
        else:
            print(f"⚠️  Refresh token expired or invalid: {data.get('message')}")
            print("   Performing full login...")
            return full_login()
            
    except Exception as e:
        print(f"⚠️  Error refreshing session: {e}")
        print("   Attempting full login...")
        return full_login()

def ensure_valid_session():
    """Ensure we have a valid session (auto-refresh if needed)"""
    global last_token_refresh
    
    if auth_token and last_token_refresh:
        time_since_refresh = datetime.now() - last_token_refresh
        if time_since_refresh < timedelta(hours=5):
            return smart_api
    
    refresh_session()
    return smart_api

def schedule_token_refresh():
    """Schedule automatic token refresh every 5 hours"""
    schedule.every(5).hours.do(refresh_session)
    
    def run_scheduler():
        while True:
            schedule.run_pending()
            time_module.sleep(60)
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print(f"⏰ Auto-refresh scheduled every 5 hours")

# Initialize session on startup
if all([api_key, client_id, password, totp_token]):
    try:
        if load_tokens_from_file() and refresh_token:
            print("🚀 Using saved refresh token...")
            refresh_session()
        else:
            print("🚀 No valid saved tokens, performing initial login...")
            full_login()
        
        schedule_token_refresh()
        print("✅ API initialized successfully! Will work for ~30 days without re-login!")
        
    except Exception as e:
        print(f"❌ Failed to initialize: {e}")
else:
    print("❌ Cannot initialize: Missing Angel One credentials")
    print("Create a .env file with ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_TOTP_TOKEN")

@app.get("/")
async def health_check():
    token_age = None
    if last_token_refresh:
        token_age = str(datetime.now() - last_token_refresh)
    
    return {
        "status": "healthy",
        "service": "Angel One Stock Data API (30-day persistent)",
        "timestamp": str(datetime.now()),
        "session_active": auth_token is not None,
        "refresh_token_available": refresh_token is not None,
        "last_refresh": str(last_token_refresh) if last_token_refresh else None,
        "token_age": token_age,
        "credentials_loaded": bool(all([api_key, client_id, password, totp_token]))
    }

@app.post("/get-ohlc")
async def get_ohlc(request: OHLCRequest):
    """Get OHLC data for a single stock symbol"""
    try:
        api = ensure_valid_session()
        
        if not api:
            raise HTTPException(status_code=500, detail="Angel One session not initialized")
        
        ohlc_data = api.ltpData(
            exchange=request.exchange,
            tradingsymbol=request.tradingSymbol,  # lowercase: tradingsymbol
            symboltoken=request.symbolToken
        )
        
        if ohlc_data and ohlc_data.get('status'):
            data = ohlc_data.get('data', {})
            response = {
                "status": True,
                "message": "SUCCESS",
                "errorcode": "",
                "data": {
                    "fetched": [{
                        "exchange": request.exchange,
                        "tradingSymbol": request.tradingSymbol,
                        "symbolToken": request.symbolToken,
                        "ltp": data.get('ltp', 0),
                        "open": data.get('open', 0),
                        "high": data.get('high', 0),
                        "low": data.get('low', 0),
                        "close": data.get('close', 0)
                    }],
                    "unfetched": []
                }
            }
            return response
        else:
            raise HTTPException(status_code=500, detail=ohlc_data.get('message', 'Failed to fetch OHLC data'))
            
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching OHLC data: {str(e)}")


@app.post("/get-ohlc-batch")
async def get_ohlc_batch(request: OHLCBatchRequest):
    """Get OHLC data for multiple stock symbols"""
    try:
        api = ensure_valid_session()
        
        if not api:
            raise HTTPException(status_code=500, detail="Angel One session not initialized")
        
        fetched = []
        unfetched = []
        
        for symbol in request.symbols:
            try:
                ohlc_data = api.ltpData(
                    exchange=symbol.get('exchange', 'NSE'),
                    tradingsymbol=symbol['tradingSymbol'],  # lowercase: tradingsymbol
                    symboltoken=symbol['symbolToken']
                )
                
                if ohlc_data and ohlc_data.get('status'):
                    data = ohlc_data.get('data', {})
                    fetched.append({
                        "exchange": symbol.get('exchange', 'NSE'),
                        "tradingSymbol": symbol['tradingSymbol'],
                        "symbolToken": symbol['symbolToken'],
                        "ltp": data.get('ltp', 0),
                        "open": data.get('open', 0),
                        "high": data.get('high', 0),
                        "low": data.get('low', 0),
                        "close": data.get('close', 0)
                    })
                else:
                    unfetched.append(symbol['tradingSymbol'])
            except:
                unfetched.append(symbol['tradingSymbol'])
        
        return {
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {
                "fetched": fetched,
                "unfetched": unfetched
            }
        }
            
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching OHLC data: {str(e)}")

@app.post("/get-market-movers")
async def get_market_movers(request: MarketMoversRequest):
    """Get top gainers/losers data"""
    try:
        api = ensure_valid_session()
        
        if not api:
            raise HTTPException(status_code=500, detail="Angel One session not initialized")
        
        movers_data = api.marketData(
            mode=request.datatype,
            exchangeTokens={"NFO": []}
        )
        
        if movers_data and movers_data.get('status'):
            return {
                "status": True,
                "message": "SUCCESS",
                "errorcode": "",
                "data": movers_data.get('data', [])
            }
        else:
            raise HTTPException(status_code=500, detail=movers_data.get('message', 'Failed to fetch market movers'))
            
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=501, detail=f"Market movers endpoint needs Angel One API adjustment: {str(e)}")

@app.get("/session-status")
async def session_status():
    """Get current session status"""
    token_age = None
    refresh_age = None
    
    if last_token_refresh:
        token_age = str(datetime.now() - last_token_refresh)
        refresh_age = (datetime.now() - last_token_refresh).days
    
    return {
        "session_active": smart_api is not None,
        "auth_token_exists": auth_token is not None,
        "refresh_token_exists": refresh_token is not None,
        "last_token_refresh": str(last_token_refresh) if last_token_refresh else None,
        "token_age": token_age,
        "days_since_refresh": refresh_age,
        "days_until_expiry": 30 - refresh_age if refresh_age is not None else None,
        "current_time": str(datetime.now()),
        "credentials_status": {
            "api_key_loaded": bool(api_key),
            "client_id_loaded": bool(client_id),
            "password_loaded": bool(password),
            "totp_token_loaded": bool(totp_token)
        }
    }

@app.post("/refresh-session")
async def manual_refresh_session():
    """Manually refresh the session"""
    try:
        refresh_session()
        return {
            "message": "Session refreshed successfully",
            "timestamp": str(datetime.now()),
            "last_refresh": str(last_token_refresh)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error refreshing session: {str(e)}")

@app.post("/force-full-login")
async def force_full_login():
    """Force a full TOTP login"""
    try:
        full_login()
        return {
            "message": "Full login completed successfully",
            "timestamp": str(datetime.now()),
            "last_refresh": str(last_token_refresh),
            "refresh_token_saved": bool(refresh_token)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during full login: {str(e)}")

@app.delete("/delete-saved-tokens")
async def delete_saved_tokens():
    """Delete saved tokens file"""
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            return {"message": "Saved tokens deleted successfully"}
        else:
            return {"message": "No saved tokens found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting tokens: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    import os
    
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)