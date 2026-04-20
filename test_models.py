import os
from dotenv import load_dotenv
from crewai import LLM

load_dotenv()

def test_ollama_gemma():
    print("Testing Ollama (gemma2:9b) — local, free...")
    llm = LLM(model="ollama/gemma2:9b", base_url="http://localhost:11434", temperature=0)
    response = llm.call([{"role": "user", "content": "Reply with only: GEMMA_OK"}])
    assert "GEMMA" in response.upper() or "OK" in response.upper(), f"Unexpected: {response}"
    print(f"  PASS — got: {response.strip()}")

def test_deepseek():
    print("Testing DeepSeek API — cloud, pay-per-use...")
    key = os.getenv("DEEPSEEK_API_KEY")
    assert key and key != "your_key_here", "DEEPSEEK_API_KEY not set in .env"
    llm = LLM(
        model="deepseek/deepseek-chat",
        api_key=key,
        base_url="https://api.deepseek.com/v1",
        temperature=0,
    )
    try:
        response = llm.call([{"role": "user", "content": "Reply with only: DEEPSEEK_OK"}])
        assert "DEEPSEEK" in response.upper() or "OK" in response.upper(), f"Unexpected: {response}"
        print(f"  PASS — got: {response.strip()}")
        return True
    except Exception as e:
        if "402" in str(e) or "Insufficient Balance" in str(e):
            print("  SKIP — DeepSeek account has no balance. Top up at platform.deepseek.com to enable.")
        else:
            print(f"  FAIL — {e}")
        return False

if __name__ == "__main__":
    ollama_ok = True
    deepseek_ok = False

    test_ollama_gemma()
    deepseek_ok = test_deepseek()

    print()
    if deepseek_ok:
        print("Both models ready. main.py will use Gemma for Analyst/Strategist and DeepSeek for Manager.")
    else:
        print("Only Ollama is ready. To run fully local, Manager can also use Gemma (free, no account needed).")
        print("To switch: open main.py and change  MANAGER_MODEL = deepseek_llm  to  MANAGER_MODEL = ollama_llm")
