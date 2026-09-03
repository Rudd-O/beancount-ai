import sys

from colorama import Fore, Style  # type: ignore


def print_diff(diff: list[str]) -> None:
    print("--- Changes ---", file=sys.stdout)
    for line in diff:
        if line.startswith("-"):
            line = Fore.RED + line + Style.RESET_ALL
        elif line.startswith("+"):
            line = Fore.GREEN + line + Style.RESET_ALL
        sys.stdout.write(line)
