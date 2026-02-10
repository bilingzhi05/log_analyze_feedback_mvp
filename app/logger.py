import datetime
import os
import sys

class Logger:
    _instance = None
    _log_file = "tool_execution.log"

    @staticmethod
    def init(log_file_path):
        """Initialize the logger with a file path."""
        Logger._log_file = log_file_path
        # Create directory if it doesn't exist
        log_dir = os.path.dirname(os.path.abspath(log_file_path))
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
            except Exception as e:
                print(f"Warning: Could not create log directory {log_dir}: {e}")

    @staticmethod
    def log(message, print_to_console=True):
        """
        Logs a message to the configured file and optionally prints to console.
        Adds a timestamp to the message.
        """
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        
        if print_to_console:
            print(formatted_message)
            
        try:
            with open(Logger._log_file, "a", encoding="utf-8") as f:
                f.write(formatted_message + "\n")
        except Exception as e:
            # Fallback if file writing fails
            print(f"Error writing to log file {Logger._log_file}: {e}")

# Helper function for easy import and usage
def log(message, log_file=None):
    """
    Simple wrapper to log a message. 
    If log_file is provided, it initializes/updates the log file path.
    """
    if log_file:
        Logger.init(log_file)
    Logger.log(message)

if __name__ == "__main__":
    # Test
    log("This is a test log message", "test.log")
    log("Another message")
