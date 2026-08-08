"""Logic with no Discord dependency.

Nothing in this package imports ``discord``. Filter matching, risk scoring, time
parsing and image generation are the parts most likely to harbour subtle bugs,
and keeping them free of gateway objects means they can be exercised without
one.
"""
