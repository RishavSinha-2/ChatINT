__version__ = "0.5.3"


def main():
    from chatint.main import main as _main
    return _main()


__all__ = ["__version__", "main"]
