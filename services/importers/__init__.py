"""Parsers for data belonging to the bots Deezee replaces.

Every module here is pure parsing: it takes text, JSON or a dict shaped like a
Discord embed, and returns plain Python. None of them import discord.py, none of
them touch the database, and none of them write anything anywhere. That is what
makes them testable, and it is also what makes the safety property easy to
state: **an importer cannot change your server, because an importer cannot do
anything but parse.**

The cog decides what to stage; ``?import review`` decides what goes live.
"""
