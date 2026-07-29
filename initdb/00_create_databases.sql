CREATE DATABASE n8n OWNER jobos;
CREATE DATABASE job_apply_os OWNER jobos;

\connect job_apply_os;

CREATE EXTENSION IF NOT EXISTS vector;
