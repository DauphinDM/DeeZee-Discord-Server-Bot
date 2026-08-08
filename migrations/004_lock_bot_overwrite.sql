-- 004_lock_bot_overwrite: keep the bot able to speak in a channel it locked.
--
-- Locking denies Send Messages to @everyone, and that overwrite applies to the
-- bot as well: guild-level role permissions lose to a channel-level deny. The
-- lock therefore succeeded while the confirmation message failed with 403, and
-- the error handler's own reply failed for the same reason -- so the command
-- looked like it did nothing.
--
-- The fix is an explicit member overwrite granting the bot Send Messages while
-- the lock is in force. This column records what that overwrite was beforehand
-- so ?unlock puts it back rather than leaving a permission the channel never had.
--
-- This file is shipped. Never edit it -- corrections go in a new migration.

-- 1 allow, 0 deny, NULL unset -- the bot's own send_messages overwrite before
-- the lock was applied.
ALTER TABLE channel_locks ADD COLUMN previous_bot INTEGER;
