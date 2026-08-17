#!/bin/bash
# Control script for Kala Voice Assistant

KALA_DIR="/home/spiderjoker/Kala_cala"
SERVICE_NAME="kala.service"

case "$1" in
    start)
        echo "Importing DISPLAY and starting Kala service..."
        # Import active X11 environment variables to allow GUI actions
        systemctl --user import-environment DISPLAY DBUS_SESSION_BUS_ADDRESS XAUTHORITY
        systemctl --user enable $SERVICE_NAME
        systemctl --user start $SERVICE_NAME
        echo "Kala service successfully started in the background."
        echo "To view live logs, run: $0 logs"
        ;;
    stop)
        echo "Stopping Kala service..."
        systemctl --user stop $SERVICE_NAME
        echo "Kala service stopped."
        ;;
    restart)
        echo "Restarting Kala service..."
        systemctl --user import-environment DISPLAY DBUS_SESSION_BUS_ADDRESS XAUTHORITY
        systemctl --user restart $SERVICE_NAME
        echo "Kala service restarted."
        ;;
    status)
        systemctl --user status $SERVICE_NAME
        ;;
    logs)
        echo "Tailing Kala service logs. Press Ctrl+C to exit."
        tail -n 100 -f "$KALA_DIR/state/kala.log"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac

exit 0
