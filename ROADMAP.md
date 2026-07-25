# 🗺️ Project Roadmap & Multi-Account Architecture

[![Status](https://img.shields.io/badge/Roadmap-v5.0_Vision-blue.svg)](#-phase-1-multi-account-configuration-registry)
[![Multi-Tenant](https://img.shields.io/badge/Multi--Tenant-Up_to_20_Accounts-purple.svg)](#-multi-account-architecture-overview)

This document outlines the strategic roadmap for scaling **Foundation of Change Coursework Automator** from a single-user tool to a **multi-account hosting platform** capable of managing coursework automation for up to **20 concurrent users** on a single machine.

---

## 💡 Vision & Primary Use Case

Many individuals who need to complete required CBT community service coursework do not have dedicated computers, reliable internet connections, or the technical setup to run automation scripts.

### **The Multi-Tenant Host Solution**
A machine owner (Host User) can manage and automate coursework for up to **20 different accounts** simultaneously:
- 👥 **Friends, Family & Community Assistance**: Host coursework for users without computers.
- ⚡ **Centralized Management**: One power user manages all credentials, schedules, and daily hour limits.
- 🍏 **Unified macOS Menu Bar Dashboard**: Switch between active user accounts in real time, view aggregated progress, and toggle workers with one click.

---

## 🏗️ Multi-Account Architecture Overview

```
                        ┌──────────────────────────────────────────┐
                        │   macOS Menu Bar Dashboard (menubar.py)  │
                        │   • Account Selector Dropdown            │
                        │   • Live Multi-Worker Progress Grid      │
                        │   • Host Controls (Start/Pause All)      │
                        └────────────────────┬─────────────────────┘
                                             │ Reads Multi-Account Events
                                             ▼
                        ┌──────────────────────────────────────────┐
                        │ Multi-Account Orchestrator Pool Manager  │
                        │        (orchestrator_pool.py)            │
                        └──────┬─────────────┬─────────────┬───────┘
                               │             │             │
              ┌────────────────┘             │             └────────────────┐
              ▼                              ▼                              ▼
   ┌────────────────────┐         ┌────────────────────┐         ┌────────────────────┐
   │ Worker #1 (User A) │         │ Worker #2 (User B) │         │ Worker #20(User T) │
   │ • Headless Chrome  │         │ • Headless Chrome  │         │ • Headless Chrome  │
   │ • Session A state  │         │ • Session B state  │         │ • Session T state  │
   │ • events_A.jsonl   │         │ • events_B.jsonl   │         │ • events_T.jsonl   │
   └────────────────────┘         └────────────────────┘         └────────────────────┘
```

---

## 📅 Roadmap Phases

### 🎯 Phase 1: Multi-Account Configuration Registry (`v4.5`)
- [x] Single-account Headless engine with Gemini Flash AI.
- [x] Native macOS Menu Bar status app with live timers and local 12-hour AM/PM formatting.
- [ ] **Multi-Credential Store (`config/accounts.json`)**: Support defining up to 20 account profiles with isolated credentials and target hours.
- [ ] **Isolated Session Storage**: Store auth cookies in `.auth_state_<user_id>.json` to ensure zero session collisions.

```json
{
  "max_concurrent_workers": 5,
  "accounts": [
    {
      "id": "user_01",
      "email": "user1@example.com",
      "password_env": "TFC_PASS_USER1",
      "target_hours": 75.0,
      "daily_limit": 8.0,
      "enabled": true
    },
    {
      "id": "user_02",
      "email": "user2@example.com",
      "password_env": "TFC_PASS_USER2",
      "target_hours": 40.0,
      "daily_limit": 8.0,
      "enabled": true
    }
  ]
}
```

---

### 🚀 Phase 2: Parallel Worker Pool Orchestrator (`v5.0`)
- [ ] **Worker Process Pool Manager (`orchestrator.py`)**: Spawns up to 20 lightweight Playwright headless workers.
- [ ] **Resource & Memory Throttling**:
  - Headless Chromium instances use < 80MB RAM per worker.
  - Staggered worker startup intervals (15 seconds apart) to keep CPU utilization minimal.
  - Adaptive queue balancing (prioritizes accounts with closest daily limit reset).
- [ ] **Per-Account Event Streams**: Writes `events_<user_id>.jsonl` and `logs/<user_id>.log` for granular auditing.

---

### 🍏 Phase 3: Multi-User macOS Menu Bar Dashboard (`v5.5`)
- [ ] **Account Switcher Dropdown**: Click to switch menu bar focus between active users:
  ```text
  👤 Active Account: [ Jane Doe (User #2) ▾ ]
  -----------------------------------------
  📌 Status: 📖 Reading Lesson #5 (14m remaining)
  📊 Account Progress: 24.5h / 75.0h (32%)
  -----------------------------------------
  👥 All Active Workers (4 Running / 2 Limit Wait):
    • Jane Doe:     🟢 Reading (14m left)
    • Alex Smith:   🟢 Reflecting
    • Sam Wilson:   🌙 Limit Wait (Reset at 12:00 AM)
    • Chris Lee:    ⏸️ Paused
  -----------------------------------------
  ▶️ Start All Workers | ⏸️ Pause All Workers
  ```
- [ ] **Global Overview Notifications**: System notifications for any account reaching their daily 8.0h limit or completing their course requirement.

---

### 🌐 Phase 4: Remote Request Portal & Non-Technical User Access (`v6.0`)
- [ ] **Lightweight Web Status Portal**: Host a local FastAPI dashboard (`http://localhost:8080`) where account owners can log in to view their status without accessing the host machine.
- [ ] **Remote Job Submission**: Friends or family can submit account requests directly to your host automator pool.
- [ ] **Automatic Certificate Exporter**: Automatically downloads PDF completion proof certificates (`enrollment-proof.pdf`) upon 100% course completion and emails them to the account owner.

---

## 🛡️ Security & Privacy Standards for Multi-User Hosting

1. **Credential Isolation**: Account passwords are read strictly from environment variables or encrypted local storage; never logged in text files.
2. **Zero PII Exposure**: All generated logs (`logs/`), events (`events/`), and session tokens (`.auth_state_*.json`) are gitignored.
3. **Independent Anti-Logout Engine**: Each account worker maintains its own independent 2.75-minute micro-scroll keep-alive loop.

---

## 🤝 Contributing & Ideas

Have suggestions for multi-account management features? Open an issue or discussion on the [GitHub Repository](https://github.com/dustindog101/tfc-coursework-automator).
