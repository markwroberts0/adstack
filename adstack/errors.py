"""Exceptions that Adstack reports to people as plain messages, without a traceback."""


class AdstackError(Exception):
    """Base class for expected failures: invalid configuration, unsafe plans, API errors."""


class ConfigError(AdstackError):
    """The configuration is invalid."""


class PlanError(AdstackError):
    """A plan cannot be created or applied safely."""


class ProviderError(AdstackError):
    """The ad platform (or the local sandbox) rejected a request or could not be reached."""
