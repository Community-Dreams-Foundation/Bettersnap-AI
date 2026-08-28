-- 027_catalog_tables.sql
-- Gender-aware catalog moved into the DB (previously hardcoded in shared/catalog.py).
-- Three tables: categories, attires (gender-specific), backgrounds (shared per category).
-- The authored seed (data/catalog_seed.json) is the single source; scripts/seed_catalog.py
-- upserts these rows AND regenerates shared/catalog.py so the GPU container (which bakes
-- catalog.py) keeps generating from the exact same data. Idempotent: safe to re-run.

-- ── Categories ────────────────────────────────────────────────────────────────
IF OBJECT_ID('dbo.catalog_categories', 'U') IS NULL
    CREATE TABLE dbo.catalog_categories (
        category_key   VARCHAR(64)   NOT NULL PRIMARY KEY,   -- snake_case, e.g. 'business_suit'
        label          NVARCHAR(128) NOT NULL,               -- 'Business Suit'
        category_type  VARCHAR(16)   NOT NULL,               -- 'professional' | 'personal'
        lead_phrase    NVARCHAR(256) NOT NULL,               -- prompt lead-in
        lighting_json  NVARCHAR(MAX) NULL,                   -- optional JSON array of lighting overrides
        is_custom      BIT           NOT NULL DEFAULT 0,     -- custom_scene: no attire/bg menu
        sort_order     INT           NOT NULL DEFAULT 0,
        is_active      BIT           NOT NULL DEFAULT 1
    );

-- ── Attires (gender-specific) ─────────────────────────────────────────────────
IF OBJECT_ID('dbo.catalog_attires', 'U') IS NULL
    CREATE TABLE dbo.catalog_attires (
        attire_id      INT IDENTITY(1,1) PRIMARY KEY,
        category_key   VARCHAR(64)   NOT NULL,
        gender         VARCHAR(8)    NOT NULL,               -- 'male' | 'female' | 'other'
        attire_key     VARCHAR(64)   NOT NULL,               -- snake_case, unique within (category, gender)
        ref            VARCHAR(160)  NOT NULL,               -- 'category_key.attire_key' (resolved WITH gender)
        label          NVARCHAR(128) NOT NULL,               -- 'Navy Suit & Tie'
        prompt_phrase  NVARCHAR(512) NOT NULL,               -- curated, gender-specific generation phrase
        image_key      NVARCHAR(128) NULL,                   -- kebab of label; path = /catalog/<gender>/attires/<image_key>_<gender>.jpg
        sort_order     INT           NOT NULL DEFAULT 0,
        is_active      BIT           NOT NULL DEFAULT 1,
        CONSTRAINT FK_attire_category FOREIGN KEY (category_key) REFERENCES dbo.catalog_categories (category_key),
        CONSTRAINT UX_attire_cat_gender_key UNIQUE (category_key, gender, attire_key)
    );
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_attire_cat_gender' AND object_id = OBJECT_ID('dbo.catalog_attires'))
    CREATE INDEX IX_attire_cat_gender ON dbo.catalog_attires (category_key, gender, is_active);

-- ── Backgrounds (shared across genders within a category) ──────────────────────
IF OBJECT_ID('dbo.catalog_backgrounds', 'U') IS NULL
    CREATE TABLE dbo.catalog_backgrounds (
        background_id  INT IDENTITY(1,1) PRIMARY KEY,
        category_key   VARCHAR(64)   NOT NULL,
        background_key VARCHAR(64)   NOT NULL,               -- snake_case, unique within category
        ref            VARCHAR(160)  NOT NULL,               -- 'category_key.background_key'
        label          NVARCHAR(128) NOT NULL,               -- 'Light Gray Studio'
        prompt_phrase  NVARCHAR(512) NOT NULL,               -- curated generation phrase
        image_key      NVARCHAR(128) NULL,                   -- kebab of label; path = /catalog/<gender>/backgrounds/<image_key>_<gender>.jpg
        sort_order     INT           NOT NULL DEFAULT 0,
        is_active      BIT           NOT NULL DEFAULT 1,
        CONSTRAINT FK_bg_category FOREIGN KEY (category_key) REFERENCES dbo.catalog_categories (category_key),
        CONSTRAINT UX_bg_cat_key UNIQUE (category_key, background_key)
    );
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_bg_cat' AND object_id = OBJECT_ID('dbo.catalog_backgrounds'))
    CREATE INDEX IX_bg_cat ON dbo.catalog_backgrounds (category_key, is_active);
