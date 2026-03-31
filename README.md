# AI-Powered Schedule Planner (智能排程助手)

An intelligent scheduling agent driven by LLMs. Users can interact with the agent using natural language (e.g., "Schedule a meeting at 3pm tomorrow") to manage their tasks, resolve scheduling conflicts automatically, and track their productivity.

## 🚀 Key Features

- **Natural Language Intent Extraction**: Supports flexible human-like commands to insert, update, or delete tasks.
- **Domino Scheduling Algorithm**: Automatically shifts overlapping tasks to resolve conflicts.
- **ReAct-style Agent**: Uses a tool-calling loop for robust context-aware planning.
- **Productivity Tracking**: Integrated timers, activity matrices, and automated reports (daily/weekly/monthly).
- **Safe & Reversible**: Comprehensive snapshot system allowing one-click rollback of any state change.

## 🛠 Prerequisites

- **Python**: 3.8 or higher.
- **Git**: For version control.
- **Supabase**: A Supabase project for data persistence.
- **LLM API Keys**: OpenAI, Gemini, or Azure OpenAI.

## 📦 How to Run

### Step 1: Clone and Install
```bash
git clone https://github.com/Nuerjibieke/schedule_planner.git
cd schedule_planner
pip install -r requirements.txt
```

### Step 2: Configuration
Copy the template and fill in your API keys in the `.env` file:
```bash
cp .env.example .env
# Edit .env with your keys
```

### Step 3: Run the Agent
Start the Streamlit interface:
```bash
streamlit run app.py
```

### Step 4: Run the Reminder Daemon (Optional)
To receive background notifications:
```bash
python run_reminder_daemon.py
```

## 📐 System Architecture

The system follows a layered architecture:
- **UI Layer**: Streamlit-based interactive dashboard.
- **AI Layer**: ReAct-style Planner with Tool-calling capabilities.
- **Logic Layer**: Task scheduler and Graph Intent resolver.
- **Data Layer**: Supabase (PostgreSQL) for persistence.

---
*This project is part of the Lab Project Archive.*
