PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  user_id      INTEGER PRIMARY KEY,
  username     TEXT,
  display_name TEXT,
  tz           TEXT NOT NULL DEFAULT 'UTC',
  birth_day    SMALLINT,
  birth_month  SMALLINT,
  birth_year   INTEGER,
  alert_hours  INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS friends (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id     INTEGER NOT NULL,
  friend_id    INTEGER,
  friend_name  TEXT,
  birth_day    SMALLINT,
  birth_month  SMALLINT,
  birth_year   INTEGER,
  UNIQUE(owner_id, friend_id),
  FOREIGN KEY(owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
  FOREIGN KEY(friend_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS groups (
  group_id     TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  creator_id   INTEGER NOT NULL,
  code         TEXT UNIQUE NOT NULL,
  FOREIGN KEY(creator_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_members (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id  TEXT NOT NULL,
  user_id   INTEGER NOT NULL,
  UNIQUE(group_id, user_id),
  FOREIGN KEY(group_id) REFERENCES groups(group_id) ON DELETE CASCADE,
  FOREIGN KEY(user_id)  REFERENCES users(user_id)  ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_chats (
  user_id   INTEGER PRIMARY KEY,
  chat_id   INTEGER NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS notifications_sent (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id    INTEGER NOT NULL,
  date_ymd     TEXT    NOT NULL,
  UNIQUE(person_id, date_ymd),
  FOREIGN KEY(person_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_users_bday ON users(birth_month, birth_day);
CREATE INDEX IF NOT EXISTS idx_groups_code ON groups(code);
CREATE INDEX IF NOT EXISTS idx_members_group ON group_members(group_id);
