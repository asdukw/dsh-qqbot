export type UsageDay = {
  day: string;
  input: number;
  cache_read: number;
  output: number;
  total: number;
  events: number;
};

export type UsageBucket = UsageDay & { purpose: string };

export type UsageSession = {
  conversation_id: string;
  session_id: string;
  input: number;
  cache_read: number;
  output: number;
  total: number;
  events: number;
};

export type UsageConversation = {
  id: string;
  label: string;
  input: number;
  cache_read: number;
  output: number;
  total: number;
};

export type UsageData = {
  days: number;
  conversation_id: string;
  totals: {
    input: number;
    cache_read: number;
    output: number;
    total: number;
    events: number;
    cache_hit_rate: number;
  };
  by_day: UsageDay[];
  by_purpose: UsageBucket[];
  by_session: UsageSession[];
  conversations: UsageConversation[];
  recent: Array<{
    created_at: number;
    source: string;
    purpose: string;
    conversation_id: string;
    session_id: string;
    provider: string;
    model: string;
    input: number;
    cache_read: number;
    output: number;
    total: number;
  }>;
};

export type FunData = {
  date: string;
  start: string;
  end: string;
  days: number;
  conversation_id: string;
  messages: number;
  ranking: Array<{ user_id: number; name: string; messages: number }>;
  words: Array<{ text: string; count: number }>;
  conversations: Array<{ id: string; label: string; messages: number }>;
  conversation_total: number;
};

export type TrendPoint = {
  day: string;
  messages: number;
  tokens: number;
};

export type OverviewData = {
  generated_at: number;
  days: number;
  active_model: string;
  active_card: string;
  counts: {
    conversations: number;
    messages: number;
    models: { total: number; enabled: number };
    cards: { total: number };
    tasks: { total: number; enabled: number };
    feeds: { total: number; errors: number };
    stickers: { total: number; banned: number };
  };
  today: {
    messages: number;
    conversations: number;
    tokens: number;
    calls: number;
  };
  trend: TrendPoint[];
  alerts: Array<{
    kind: string;
    id: number;
    title: string;
    detail: string;
    at: number | null;
  }>;
  recent_messages: Array<{
    conversation_id: string;
    label: string;
    sender_name: string;
    text: string;
    created_at: number;
  }>;
};

export type ModelRow = {
  provider: string;
  model_id: string;
  display_name: string;
  api: string;
  base_url: string;
  context_window: number;
  max_output_tokens: number;
  reasoning_effort: string;
  capabilities: Record<string, unknown>;
  enabled: boolean;
  is_active: boolean;
  updated_at: number;
  provider_display_name: string;
  provider_enabled: boolean;
  builtin: boolean;
};

export type ProviderRow = {
  provider: string;
  display_name: string;
  api: string;
  base_url: string;
  enabled: boolean;
  builtin: boolean;
  model_count: number;
  enabled_models: number;
  active_model_id: string | null;
  api_key_set: boolean;
  api_key_source: "stored" | "env" | "missing";
  updated_at: number;
};

export type ProviderPreset = {
  provider: string;
  display_name: string;
  api: string;
  base_url: string;
  builtin: boolean;
};

export type ModelsData = {
  generated_at: number;
  active: string;
  providers: ProviderRow[];
  models: ModelRow[];
  presets: ProviderPreset[];
};

export type CardRow = {
  id: number;
  name: string;
  nickname: string;
  aliases: string[];
  description: string;
  description_chars: number;
  tags: string[];
  creator: string;
  character_version: string;
  source_kind: string;
  source: string;
  installed_by: string;
  is_active: boolean;
  created_at: number;
  updated_at: number;
};

export type CardsData = {
  generated_at: number;
  active_card: string;
  cards: CardRow[];
};

// Character Card V3 `data` as stored by bot/cards.py. Fields are optional
// because legacy rows may carry an empty payload.
export type LoreEntryV3 = {
  keys?: string[];
  secondary_keys?: string[];
  content?: string;
  extensions?: Record<string, unknown>;
  enabled?: boolean;
  insertion_order?: number;
  case_sensitive?: boolean;
  use_regex?: boolean;
  constant?: boolean;
  name?: string;
  comment?: string;
  priority?: number;
  id?: number | string;
  selective?: boolean;
  position?: string;
};

export type CharacterBookV3 = {
  name?: string;
  description?: string;
  scan_depth?: number;
  token_budget?: number;
  recursive_scanning?: boolean;
  extensions?: Record<string, unknown>;
  entries?: LoreEntryV3[];
};

export type CardAssetV3 = {
  type?: string;
  uri?: string;
  name?: string;
  ext?: string;
};

export type CardDataV3 = {
  name?: string;
  nickname?: string;
  description?: string;
  personality?: string;
  scenario?: string;
  first_mes?: string;
  mes_example?: string;
  creator_notes?: string;
  creator_notes_multilingual?: Record<string, string>;
  system_prompt?: string;
  post_history_instructions?: string;
  alternate_greetings?: string[];
  group_only_greetings?: string[];
  character_book?: CharacterBookV3;
  tags?: string[];
  creator?: string;
  character_version?: string;
  extensions?: Record<string, unknown>;
  assets?: CardAssetV3[];
  source?: string[];
  creation_date?: number;
  modification_date?: number;
};

export type CardDetail = {
  generated_at: number;
  id: number;
  name: string;
  nickname: string;
  aliases: string[];
  tags: string[];
  creator: string;
  character_version: string;
  fingerprint: string;
  source_kind: string;
  source: string;
  installed_by: string;
  is_active: boolean;
  created_at: number;
  updated_at: number;
  spec: string;
  spec_version: string;
  imported_from: { spec?: string; spec_version?: string };
  data: CardDataV3;
};

export type TaskRow = {
  id: number;
  chat_type: string;
  chat_id: number;
  created_by: string;
  instruction: string;
  message: string;
  kind: string;
  time_of_day: string | null;
  weekday: number | null;
  run_at: number | null;
  every_minutes: number | null;
  mode: string;
  enabled: boolean;
  created_at: number;
  next_run: number | null;
  last_run: number;
  runs: number;
};

export type TasksData = {
  generated_at: number;
  tasks: TaskRow[];
  totals: { total: number; enabled: number };
};

export type FeedRow = {
  id: number;
  chat_type: string;
  chat_id: number;
  title: string;
  feed_url: string;
  source: string;
  seen_count: number;
  enabled: boolean;
  created_at: number;
  last_check: number;
  last_error: string;
};

export type FeedsData = {
  generated_at: number;
  feeds: FeedRow[];
  total: number;
  errors: number;
};

export type StickerRow = {
  id: number;
  description: string;
  tags: string[];
  source: string;
  source_kind: string;
  status: string;
  uses: number;
  size: number;
  mime: string;
  has_image: boolean;
  created_at: number;
  last_used: number;
};

export type StickersData = {
  generated_at: number;
  stickers: StickerRow[];
  totals: { total: number; banned: number; used: number };
};

export type SystemData = {
  generated_at: number;
  started_at: number;
  uptime_seconds: number;
  python: string;
  static_dir: string;
  admin: {
    host: string;
    port: number;
    session_hours: number;
    allow_cidrs: string[];
  };
  dsh: {
    home: string;
    workspace: string;
    profile: string;
    image_mode: string;
  };
  usage: {
    retention_days: number;
    event_retention_days: number;
  };
  databases: Array<{
    key: string;
    path: string;
    exists: boolean;
    size: number | null;
  }>;
};
