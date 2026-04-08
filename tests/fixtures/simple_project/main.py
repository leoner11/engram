"""Entry point that calls the service layer."""

from service import process_order, get_order_summary


def main():
    """Main entry point."""
    order = process_order("user_123", ["item_a", "item_b"])
    summary = get_order_summary(order.id)
    print(summary)


if __name__ == "__main__":
    main()
