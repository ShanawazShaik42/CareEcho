import os
import time
from dotenv import load_dotenv
from google import genai

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("No API key found. Check your .env file.")
else:
    client = genai.Client(api_key=api_key)
    for attempt in range(1, 5):
        try:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents="Say hello in one sentence."
            )
            print(response.text)
            break
        except Exception as e:
            print(f"Attempt {attempt} failed: {type(e).__name__}: {str(e)[:150]}")
            time.sleep(attempt * 5)