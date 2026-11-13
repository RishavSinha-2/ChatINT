__version__ = "0.5.0"


def main():
    from chatint.main import main as _main
    return _main()


__all__ = ["__version__", "main"]
