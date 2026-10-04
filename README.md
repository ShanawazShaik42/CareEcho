# CareEcho 🔊

**A voice-first AI companion that helps elderly patients in rural areas take the right medicines at the right time after hospital discharge.**

Built for the **HackNowa Global Hackathon 2026** · Domain: *AI for Everyday Life*

---

Live Demo: https://careecho.onrender.com/

## The Problem

After leaving the hospital, many elderly patients in rural areas go home with a discharge paper they cannot read and medicines they do not understand. Missed doses, wrong tablets and skipped checkups lead to avoidable readmissions. Text reminders don't help someone who cannot read.

## The Solution

CareEcho turns a discharge paper into a **spoken, step-by-step care companion** in the patient's own language.

1. **Scan** – A family member or health worker photographs the discharge paper. AI extracts the medicine schedule, diet advice and revisit dates.
2. **Remind** – At every dose time, the app speaks an alert in the patient's regional language, in simple words.
3. **Check meal** – The app asks, by voice, *"Have you eaten?"* before any tablet is taken.
4. **Verify tablets** – The patient shows their tablets to the camera. The app says which ones to take right now and which to avoid.
5. **Log and alert** – Every dose is logged. If a dose is missed, the caregiver is alerted.

## Key Features

- 🗣️ Voice-first: everything is spoken, no reading required
- 🌐 Regional language support
- 📄 Discharge paper → structured schedule using Gemini
- 🍽️ "Have you eaten?" check before each dose
- 📸 Tablet photo verification at every dose time
- 👨‍👩‍👧 Setup page for family or health workers
- 🔔 Caregiver alert on missed doses
- 🛡️ Safety guardrails: the app does not give medical advice beyond the doctor's prescription

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, Flask |
| AI | Google Gemini API (vision + language) |
| Storage | JSON (`data.json`) |
| Frontend | HTML, CSS, JavaScript (Web Speech / speech synthesis) |

## Project Structure

```
CareEcho/
├── app.py            # Flask app: routes, Gemini calls, dose logic
├── data.json         # Patient schedule and dose log
├── templates/        # /setup page and elder voice screen
├── static/           # CSS, JS, assets
├── requirements.txt
└── .env              # API keys (not committed)
```

## Getting Started

### 1. Clone the repo

```bash
git clone https://github.com/ShanawazShaik42/CareEcho.git
cd CareEcho
```

### 2. Create a virtual environment

```bash
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Add your API key

Create a `.env` file in the project root:

```
GEMINI_API_KEY=your_key_here
```

Get a free key from [Google AI Studio](https://aistudio.google.com/).

### 5. Run the app

```bash
python app.py
```

Open `http://localhost:5000`.

## How to Use

1. Open **/setup** (family member or health worker): upload the discharge paper, choose the patient's language, and confirm the extracted schedule.
2. Open **/** on the patient's phone or tablet: the voice screen takes over and guides the patient through each dose.

## Safety

CareEcho only repeats what the doctor has prescribed. It never suggests new medicines or changes doses, and it directs patients to their caregiver or doctor whenever something is unclear.

## Future Scope

- Automatic SMS or WhatsApp alerts to caregivers
- More regional languages and dialects
- Offline mode for low-connectivity villages
- Integration with hospital discharge systems
- Revisit and checkup reminders with transport help

## Author

**Shanawaz Shaik**
[GitHub](https://github.com/ShanawazShaik42)

---

*Built with ❤️ for HackNowa Global Hackathon 2026.*
