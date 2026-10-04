CREATE TABLE IF NOT EXISTS households (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  planning_time TIME DEFAULT '20:00',
  budget_daily NUMERIC NOT NULL DEFAULT 500,
  currency TEXT DEFAULT 'INR',
  pincode TEXT,
  nutrition_goal TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS members (
  id BIGSERIAL PRIMARY KEY,
  household_id TEXT REFERENCES households(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  diet TEXT,
  preferences JSONB DEFAULT '[]',
  restrictions JSONB DEFAULT '[]'
);

-- source: 'setup' = hard rules the agent may never relax; 'family'/'voice' = runtime rules a human may relax
CREATE TABLE IF NOT EXISTS household_rules (
  id BIGSERIAL PRIMARY KEY,
  household_id TEXT REFERENCES households(id) ON DELETE CASCADE,
  rule_type TEXT NOT NULL,
  rule_value JSONB NOT NULL,
  priority INT DEFAULT 1,
  source TEXT DEFAULT 'setup',
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory (
  id BIGSERIAL PRIMARY KEY,
  household_id TEXT REFERENCES households(id) ON DELETE CASCADE,
  ingredient TEXT NOT NULL,
  quantity NUMERIC NOT NULL,
  unit TEXT NOT NULL,
  expiry_date DATE,
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (household_id, ingredient)
);

-- what the (mock) grocery market sells; flip in_stock to simulate out-of-stock
CREATE TABLE IF NOT EXISTS catalog (
  ingredient TEXT PRIMARY KEY,
  unit TEXT NOT NULL,
  price_per_unit NUMERIC NOT NULL,
  in_stock BOOLEAN DEFAULT true
);

CREATE TABLE IF NOT EXISTS meals (
  id BIGSERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  ingredients JSONB NOT NULL,
  instructions TEXT,
  nutrition JSONB DEFAULT '{}',
  cost_estimate NUMERIC DEFAULT 0,
  tags JSONB DEFAULT '[]',
  diet TEXT DEFAULT 'vegetarian',
  contains JSONB DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT,
  household_id TEXT REFERENCES households(id) ON DELETE CASCADE,
  items JSONB,
  amount NUMERIC,
  status TEXT,
  payment_status TEXT,
  delivery_status TEXT,
  external_payment_id TEXT,
  external_delivery_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meal_history (
  id BIGSERIAL PRIMARY KEY,
  household_id TEXT REFERENCES households(id) ON DELETE CASCADE,
  meal_id BIGINT REFERENCES meals(id),
  date DATE NOT NULL,
  status TEXT,
  rating INT,
  feedback TEXT
);

-- checkpoint table: this is what makes crash recovery possible
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY,
  household_id TEXT REFERENCES households(id) ON DELETE CASCADE,
  current_state TEXT NOT NULL,
  last_verified_state TEXT,
  status TEXT NOT NULL,
  context JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- the decision log shown on the dashboard
CREATE TABLE IF NOT EXISTS agent_events (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT REFERENCES agent_runs(run_id) ON DELETE CASCADE,
  ts TIMESTAMPTZ DEFAULT now(),
  state TEXT,
  action TEXT,
  reason TEXT,
  result JSONB,
  next_step TEXT
);

-- events injected by the simulator ("person behind the curtain" only plays the world)
CREATE TABLE IF NOT EXISTS world_events (
  id BIGSERIAL PRIMARY KEY,
  household_id TEXT,
  kind TEXT NOT NULL,
  payload JSONB DEFAULT '{}',
  consumed BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- the mock Pine Labs / Delhivery "servers": state lives outside the agent process
CREATE TABLE IF NOT EXISTS mock_external (
  external_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  idempotency_key TEXT UNIQUE,
  status TEXT NOT NULL,
  payload JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS voice_inputs (
  id BIGSERIAL PRIMARY KEY,
  household_id TEXT,
  raw_response JSONB,
  transcript TEXT,
  interpretation JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT,
  household_id TEXT,
  channel TEXT NOT NULL, -- dashboard | email
  direction TEXT NOT NULL DEFAULT 'out',
  recipient TEXT,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT DEFAULT 'sent',
  provider_response JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
