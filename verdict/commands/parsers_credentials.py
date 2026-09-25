"""Parsers for credential management commands."""

from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register credential management commands."""
    credentials_p = subparsers.add_parser(
        "credentials",
        help="Manage API credentials and secrets"
    )
    
    credentials_sub = credentials_p.add_subparsers(
        dest="credentials_command",
        help="Credential operations"
    )
    
    # List command
    list_p = credentials_sub.add_parser(
        "list",
        help="List all registered credentials with their source and masked value"
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON"
    )
    
    # Set command
    set_p = credentials_sub.add_parser(
        "set",
        help="Set a credential value (reads from hidden prompt or --stdin)"
    )
    set_p.add_argument(
        "name",
        help="Credential name (e.g., OMNIROUTE_API_KEY)"
    )
    set_p.add_argument(
        "--stdin",
        action="store_true",
        help="Read value from stdin instead of prompt"
    )
    set_p.add_argument(
        "--force-unregistered",
        action="store_true",
        help="Allow setting unregistered credentials"
    )
    
    # Unset command
    unset_p = credentials_sub.add_parser(
        "unset",
        help="Remove a credential from the store"
    )
    unset_p.add_argument(
        "name",
        help="Credential name to remove"
    )
    
    # Test command
    test_p = credentials_sub.add_parser(
        "test",
        help="Test a credential with its live check"
    )
    test_p.add_argument(
        "name",
        help="Credential name to test"
    )
