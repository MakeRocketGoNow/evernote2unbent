class Evernote2UnbentError(Exception):
    """Anything the user can act on, reported without a traceback."""


class UnbentAuthError(Evernote2UnbentError):
    """The stored session is missing, refused, or was never created."""
