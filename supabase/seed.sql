-- Sample team for local development (`supabase db reset`). Same people the
-- backend seeds when the employees table is empty.
insert into public.employees (id, name, role, department, phone, language, notes) values
  ('seed0001', 'Amira Hassan', 'Field Technician', 'Operations', '+10000000001', 'English', ''),
  ('seed0002', 'Daniel Park',  'Sales Associate',  'Sales',      '+10000000002', 'English', ''),
  ('seed0003', 'Priya Nair',   'Nurse',            'Clinic',     '+10000000003', 'English', '')
on conflict (id) do nothing;
