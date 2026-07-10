# StationWatch

## Overview

This project provides a robust solution for monitoring agent machines, specifically designed for use in environments
like live events. It offers real-time visibility into machine status, active user sessions, and hardware integrity.

## System Architecture

The system is divided into three primary components:

| Component                                     | Responsibility                                         |
|-----------------------------------------------|--------------------------------------------------------|
| Server                                        | API endpoint and web dashboard for data visualization. |
| Agent                                         | Client-side application running on monitored machines. |
| [NTFY](https://github.com/binwiederhier/ntfy) | Push notification manager for real-time alerts.        |

## Key Features

- **Active Window Tracking:** Detects which application window is currently in focus on the agent machine.
- **Peripheral Monitoring:** Tracks connected devices to prevent unauthorized hardware changes or theft.
- **Heartbeat System:** Constant connection verification ensures the server knows immediately if an agent machine goes
  offline.
- **Instant Alerts:** Leverages NTFY to send real-time push notifications to your desktop or mobile device.⚠️ Platform
  SupportCurrently, this project is compatible with Windows systems only.