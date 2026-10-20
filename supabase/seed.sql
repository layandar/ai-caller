-- Sample team for an empty database (`supabase db reset`, or paste into the SQL
-- editor). Only inserts when the table has no one in it.
insert into public.employees (name, role, department, phone_number)
select * from (values
  ('Amira Hassan', 'Field Technician', 'Operations', '+10000000001'),
  ('Daniel Park',  'Sales Associate',  'Sales',      '+10000000002'),
  ('Priya Nair',   'Nurse',            'Clinic',     '+10000000003')
) as v(name, role, department, phone_number)
where not exists (select 1 from public.employees);
