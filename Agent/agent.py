from pydantic_settings import BaseSettings, SettingsConfigDict

from Agent import devices, helper, windows, connection


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file_encoding='utf-8', extra='ignore')

    debug: bool = False
    server_url: str = "http://localhost:3000/"
    heartbeat_interval: int = 5


settings = AgentSettings()

DEVICE_ID = windows.get_machine_guid()


def main():
    print(f"Starting Agent for {DEVICE_ID}...")
    print("Initializing hardware baseline silently...")
    devices.init()
    helper.start_thread(connection.heartbeat_loop)

    print("Ready. Only physical connect/disconnect events will stream to your dashboard.\n")
    windows.init()
    windows.start_message_pump()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
        input("Press Enter to exit...")
