
-- Create profiles table
CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  full_name TEXT,
  email TEXT,
  credits INTEGER NOT NULL DEFAULT 3,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Create headshot_jobs table
CREATE TABLE IF NOT EXISTS public.headshot_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  input_image_url TEXT,
  output_image_url TEXT,
  gender TEXT NOT NULL DEFAULT 'male',
  background TEXT NOT NULL DEFAULT 'office',
  status TEXT NOT NULL DEFAULT 'pending',
  prediction_id TEXT,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enable RLS
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.headshot_jobs ENABLE ROW LEVEL SECURITY;

-- Profiles RLS: users can read/update their own profile
CREATE POLICY "Users can view own profile" ON public.profiles
  FOR SELECT USING (auth.uid() = id);

CREATE POLICY "Users can update own profile" ON public.profiles
  FOR UPDATE USING (auth.uid() = id);

CREATE POLICY "Users can insert own profile" ON public.profiles
  FOR INSERT WITH CHECK (auth.uid() = id);

-- Headshot jobs RLS: users can CRUD their own jobs
CREATE POLICY "Users can view own jobs" ON public.headshot_jobs
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own jobs" ON public.headshot_jobs
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own jobs" ON public.headshot_jobs
  FOR UPDATE USING (auth.uid() = user_id);

CREATE POLICY "Users can delete own jobs" ON public.headshot_jobs
  FOR DELETE USING (auth.uid() = user_id);

-- Auto-create profile on signup trigger
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (id, full_name, email)
  VALUES (
    NEW.id,
    COALESCE(NEW.raw_user_meta_data ->> 'full_name', ''),
    NEW.email
  );
  RETURN NEW;
END;
$$;

-- Drop trigger if exists then create
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();
