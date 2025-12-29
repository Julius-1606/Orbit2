import os
import json
import random
import asyncio
import sys
import time
import warnings
import toml

# --- 🔇 SUPPRESS WARNINGS ---
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"
warnings.filterwarnings("ignore")

import google.generativeai as genai
from telegram import Bot
from github import Github

# --- 🔐 SECRETS MANAGEMENT ---
GEMINI_API_KEYS = []

# 1. Load Secrets from Env
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
KEYS_STRING = os.environ.get("GEMINI_KEYS")
GITHUB_PAT = os.environ.get("GITHUB_KEYS") or os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPO")

# 2. Local Fallback
if not TELEGRAM_TOKEN or not KEYS_STRING:
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        secrets_path = os.path.join(script_dir, ".streamlit", "secrets.toml")
        
        if os.path.exists(secrets_path):
            print(f"📂 Loading secrets from: {secrets_path}")
            with open(secrets_path, "r") as f:
                local_secrets = toml.load(f)
                TELEGRAM_TOKEN = TELEGRAM_TOKEN or local_secrets.get("TELEGRAM_TOKEN")
                raw_keys = local_secrets.get("GEMINI_KEYS")
                if isinstance(raw_keys, list):
                    GEMINI_API_KEYS = raw_keys
                elif isinstance(raw_keys, str):
                    GEMINI_API_KEYS = [k.strip() for k in raw_keys.split(",")]
                GITHUB_PAT = GITHUB_PAT or local_secrets.get("GITHUB_KEYS") or local_secrets.get("GITHUB_TOKEN")
                GITHUB_REPO = GITHUB_REPO or local_secrets.get("GITHUB_REPO")
    except Exception as e:
        print(f"⚠️ Local secrets error: {e}")

# 3. Final Parse
if not GEMINI_API_KEYS and KEYS_STRING:
    GEMINI_API_KEYS = [k.strip() for k in KEYS_STRING.split(",")]

GEMINI_API_KEYS = [k.strip() for k in GEMINI_API_KEYS if k.strip()]

if not TELEGRAM_TOKEN or not GEMINI_API_KEYS:
    print("❌ FATAL ERROR: Telegram Token or Gemini Keys missing.")
    sys.exit(1)

CHAT_ID = "6882899041" 
CURRENT_KEY_INDEX = 0
SELECTED_MODEL_NAME = "gemini-1.5-flash" # Default

# --- 🧠 BRAIN CONFIGURATION ---
def configure_genai():
    """Sets the active API key based on the current index."""
    global CURRENT_KEY_INDEX
    if not GEMINI_API_KEYS: return
    
    # Ensure index is within bounds (wrap around safety)
    CURRENT_KEY_INDEX = CURRENT_KEY_INDEX % len(GEMINI_API_KEYS)
    current_key = GEMINI_API_KEYS[CURRENT_KEY_INDEX]
    
    try:
        genai.configure(api_key=current_key)
        # print(f"🔑 Active Key: ...{current_key[-4:]} (Index {CURRENT_KEY_INDEX})")
    except Exception as e:
        print(f"⚠️ Config Error: {e}")

def resolve_model_name():
    """Finds the best model ONCE at startup to save API calls."""
    print("🔍 Sonar Scanning for best model...")
    try:
        # Use first key to find model
        configure_genai()
        models = list(genai.list_models())
        valid_models = [m.name for m in models if 'generateContent' in m.supported_generation_methods]
        
        # Priority 1: Flash 1.5
        for m in valid_models:
            if 'gemini-1.5-flash' in m and 'latest' not in m and 'exp' not in m:
                print(f"✅ Target Locked: {m}")
                return m.replace("models/", "")
        
        # Priority 2: Any Flash
        for m in valid_models:
             if 'flash' in m and 'gemini-2' not in m and 'exp' not in m:
                print(f"⚠️ Flash Fallback: {m}")
                return m.replace("models/", "")

        # Priority 3: Anything else
        if valid_models:
            return valid_models[0].replace("models/", "")
            
    except Exception as e:
        print(f"⚠️ Scan failed ({e}). Defaulting to Flash.")
    
    return "gemini-1.5-flash"

# Perform initial setup
configure_genai()
SELECTED_MODEL_NAME = resolve_model_name()
model = genai.GenerativeModel(SELECTED_MODEL_NAME)

def rotate_key():
    """Switches to the next key without re-scanning models."""
    global CURRENT_KEY_INDEX, model
    
    if len(GEMINI_API_KEYS) <= 1:
        print("❌ No backup keys available for rotation.")
        return False

    CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(GEMINI_API_KEYS)
    print(f"🔄 Rotating to Key #{CURRENT_KEY_INDEX + 1}...")
    
    # Re-configure with new key
    configure_genai()
    
    # Re-instantiate model to ensure it uses the new config
    # We DO NOT call resolve_model_name() here to save an API call
    model = genai.GenerativeModel(SELECTED_MODEL_NAME)
    return True

# 🛡️ SAFE GENERATOR LOOP
def generate_content_safe(prompt_text):
    global model
    # Try every key we have + 1 retry
    max_retries = len(GEMINI_API_KEYS) + 1
    
    for attempt in range(max_retries):
        try:
            return model.generate_content(prompt_text)
        except Exception as e:
            err_msg = str(e)
            
            # Detect Quota (429) or Auth (403) issues
            is_quota = "429" in err_msg or "quota" in err_msg.lower() or "ResourceExhausted" in err_msg
            is_auth = "403" in err_msg or "leaked" in err_msg.lower()
            
            if is_quota or is_auth:
                print(f"⏳ API Issue ({'Quota' if is_quota else 'Auth'}). Switching keys...")
                if rotate_key():
                    time.sleep(1) # Short breather for the new key
                    continue
                else:
                    print("❌ All keys exhausted.")
                    return None
            else:
                # If it's a 500/503 (Server Error), maybe wait and retry same key
                if "500" in err_msg or "503" in err_msg:
                    time.sleep(2)
                    continue
                
                print(f"❌ Critical Error: {err_msg}")
                return None
    return None

# 🛡️ TELEGRAM SAFETY VALVE
async def send_safe_message(bot, chat_id, text):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        print(f"⚠️ HTML Parse Error: {e}. Sending raw text.")
        await bot.send_message(chat_id=chat_id, text=text)

# --- ☁️ CONFIG LOADER ---
def load_config():
    if GITHUB_PAT and GITHUB_REPO:
        try:
            # print("☁️ Fetching config from GitHub...")
            g = Github(GITHUB_PAT)
            repo = g.get_repo(GITHUB_REPO)
            contents = repo.get_contents("config.json")
            decoded = contents.decoded_content.decode()
            return json.loads(decoded)
        except Exception as e:
            print(f"⚠️ Cloud load failed: {e}")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, 'config.json')
    try:
        with open(config_path, 'r') as f: return json.load(f)
    except FileNotFoundError: return None

async def send_chaos():
    bot = Bot(token=TELEGRAM_TOKEN)
    config = load_config()
    
    if not config:
        print("❌ Config load failed.")
        return 

    if "--quiz" in sys.argv: roll = 90
    elif "--fact" in sys.argv: roll = 60
    else: roll = random.randint(1, 100)
    print(f"🎲 Rolled a {roll}")

    if roll <= 50:
        print("Silence is golden.")
        return

    # --- FACT MODE ---
    elif 51 <= roll <= 85:
        topic = random.choice(config['interests'])
        prompt = f"Tell me a mind-blowing, short random fact about {topic}. Keep it under 2 sentences."
        response = generate_content_safe(prompt)
        if response and response.text:
            msg = f"🎱 <b>Magic-∞ Fact:</b>\n\n{response.text}"
            await send_safe_message(bot, CHAT_ID, msg)

    # --- MULTI-QUIZ MODE ---
    elif 86 <= roll <= 98:
        unit = random.choice(config['current_units'])
        num_q = random.randint(1, 5) 
        
        await send_safe_message(bot, CHAT_ID, f"🚨 <b>INCOMING CHAOS</b>\n\nRapid Fire: <b>{num_q} Questions on {unit}</b>")
        
        prompt = f"""
        Generate {num_q} multiple-choice questions about {unit} for a 4th Year Student.
        Strict JSON format: List of objects.
        [
            {{"question": "...", "options": ["A","B","C","D"], "correct_id": 0, "explanation": "..."}}
        ]
        """

        response = generate_content_safe(prompt)
        
        if response and response.text:
            try:
                text = response.text.replace('```json', '').replace('```', '').strip()
                data = json.loads(text)
                if isinstance(data, dict): data = [data]
                
                for i, q in enumerate(data):
                    try:
                        await bot.send_poll(
                            chat_id=CHAT_ID,
                            question=f"[{i+1}/{len(data)}] {q['question'][:290]}",
                            options=[o[:97] for o in q['options']],
                            type="quiz",
                            correct_option_id=q['correct_id'],
                            explanation=q['explanation'][:190]
                        )
                        time.sleep(2) 
                    except Exception as e:
                        print(f"⚠️ Poll failed: {e}")
            except Exception as e:
                print(f"Quiz Parse Error: {e}")
        else:
             print("⚠️ No response for Quiz")
             
    else:
        await send_safe_message(bot, CHAT_ID, "👑 <b>GOD MODE ACTIVATED</b>")

if __name__ == "__main__":
    asyncio.run(send_chaos())
