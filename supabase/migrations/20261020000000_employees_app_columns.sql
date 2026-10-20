-- The employees table was created in the dashboard before the first migration ran
-- (uuid id, phone_number, department, is_active), so that migration left it as is.
-- Add the columns the app's Employee model uses; existing rows get the defaults.
alter table public.employees
  add column if not exists role      text not null default '',
  add column if not exists language  text not null default 'English',
  add column if not exists notes     text not null default '';
