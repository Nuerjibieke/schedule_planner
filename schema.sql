-- Supabase 上需先执行本脚本建表

-- 任务总表
CREATE TABLE IF NOT EXISTS tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  start_time TIMESTAMPTZ NOT NULL,
  end_time TIMESTAMPTZ NOT NULL,
  title TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high','normal','low')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done')),
  is_fixed BOOLEAN NOT NULL DEFAULT FALSE,
  memo TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 快照表（撤销用）
CREATE TABLE IF NOT EXISTS snapshots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  payload JSONB NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ DEFAULT now()
);

-- 总结表（日报/周报/月报）
CREATE TABLE IF NOT EXISTS summaries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  date DATE NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('daily','weekly','monthly')),
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
-- 已有表需放宽 kind：ALTER TABLE summaries DROP CONSTRAINT IF EXISTS summaries_kind_check; ALTER TABLE summaries ADD CONSTRAINT summaries_kind_check CHECK (kind IN ('daily','weekly','monthly'));

-- 清理重复数据：对于每个(date, kind)组合，只保留created_at最新的记录
-- 删除重复记录，只保留每个(date, kind)组合中created_at最新的一条
DELETE FROM summaries
WHERE id NOT IN (
    SELECT DISTINCT ON (date, kind) id
    FROM summaries
    ORDER BY date, kind, created_at DESC
);

-- 添加唯一约束：每个日期和类型的组合只能有一条记录
ALTER TABLE summaries DROP CONSTRAINT IF EXISTS summaries_date_kind_unique;
ALTER TABLE summaries ADD CONSTRAINT summaries_date_kind_unique UNIQUE (date, kind);

-- 提醒事项表
CREATE TABLE IF NOT EXISTS reminders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content TEXT NOT NULL,
  is_completed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- 自动更新 updated_at 的触发器函数
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- 为 reminders 表创建触发器
DROP TRIGGER IF EXISTS update_reminders_updated_at ON reminders;
CREATE TRIGGER update_reminders_updated_at
    BEFORE UPDATE ON reminders
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- 便于按日期查任务
CREATE INDEX IF NOT EXISTS idx_tasks_start ON tasks (start_time);
CREATE INDEX IF NOT EXISTS idx_summaries_date_kind ON summaries (date, kind);
CREATE INDEX IF NOT EXISTS idx_reminders_created ON reminders (created_at);

-- =========================
-- 长周期任务（项目/里程碑/子任务）
-- =========================
CREATE TABLE IF NOT EXISTS long_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  parent_id UUID NULL REFERENCES long_tasks(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  description TEXT,
  status TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','doing','blocked','done','cancelled')),
  priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high','normal','low')),
  progress INT NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 100),
  start_date DATE,
  due_date DATE,
  tags TEXT[] NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS long_task_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES long_tasks(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'note' CHECK (kind IN ('note','status','progress','system')),
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- updated_at 自动更新
DROP TRIGGER IF EXISTS update_long_tasks_updated_at ON long_tasks;
CREATE TRIGGER update_long_tasks_updated_at
    BEFORE UPDATE ON long_tasks
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

CREATE INDEX IF NOT EXISTS idx_long_tasks_due_date ON long_tasks (due_date);
CREATE INDEX IF NOT EXISTS idx_long_tasks_status ON long_tasks (status);
CREATE INDEX IF NOT EXISTS idx_long_task_logs_task_created ON long_task_logs (task_id, created_at);
