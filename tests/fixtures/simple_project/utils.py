"""Utility functions used across the project."""


def format_currency(amount: float) -> str:
    """Format a number as USD currency string."""
    return f"${amount:,.2f}"


def validate_user_id(user_id: str) -> bool:
    """
    Validate that a user ID is properly formatted.
    Raises ValueError if invalid.
    """
    if not user_id or not isinstance(user_id, str):
        raise ValueError("Invalid user ID")
    if len(user_id) < 3:
        raise ValueError("User ID too short")
    return True
