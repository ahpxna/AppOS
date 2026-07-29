-- =====================================================================
-- approve_drafts.sql
--
-- Viet lai + duyet 5 project asset, tach asset gop, bo tat ca ten nguoi.
--
-- CHAY:
--   cd ~/job-apply-os
--   sudo docker exec -i jobos-postgres psql -U jobos -d job_apply_os \
--        -P pager=off -v ON_ERROR_STOP=1 < approve_drafts.sql
--
-- AN TOAN:
--   - Toan bo nam trong BEGIN ... khong co COMMIT.
--   - Chay lan dau -> tu dong ROLLBACK o cuoi, chi de XEM ket qua.
--   - Ung y roi thi bo comment dong COMMIT o cuoi file, chay lai.
--   - Da co backup: backups/state_20260728_141646.sql (13M)
-- =====================================================================

\echo ''
\echo '### 0. TRUOC KHI SUA ###'
SELECT status, asset_type, count(*) FROM profile_assets GROUP BY 1,2 ORDER BY 1,2;

\echo ''
\echo '### Kiem tra rang buoc status (phong truong hop co gia tri khac) ###'
SELECT pg_get_constraintdef(oid) AS status_constraint
FROM pg_constraint
WHERE conrelid = 'profile_assets'::regclass
  AND pg_get_constraintdef(oid) ILIKE '%status%';

BEGIN;

-- =====================================================================
-- 1. BO TEN NGUOI KHOI TOAN BO DATABASE
--    Day la PII cua nguoi khac trong he thong se tu dong gui ra ngoai.
--    Doi thanh mo ta trung tinh, khong mat thong tin nao can dung.
-- =====================================================================

UPDATE profile_assets
SET do_not_overclaim_rules = (
    SELECT array_agg(
        regexp_replace(
          regexp_replace(
            regexp_replace(
              regexp_replace(
                regexp_replace(r,
                  ',?\s*with\s+(Alyssa Morello|Vianney Santos Bravo|Vianney Santos|Stalin Ochoa|Eric Megargee|John Tomaszewski)(\s*,\s*(Alyssa Morello|Vianney Santos Bravo|Vianney Santos|Stalin Ochoa|Eric Megargee|John Tomaszewski))*',
                  '', 'gi'),
                '(Alyssa Morello|Vianney Santos Bravo|Vianney Santos|Stalin Ochoa|Eric Megargee|John Tomaszewski)(\s*,\s*(Alyssa Morello|Vianney Santos Bravo|Vianney Santos|Stalin Ochoa|Eric Megargee|John Tomaszewski))*',
                '', 'gi'),
              '\(\s*[,;]?\s*\)', '', 'g'),
            ',\s*\)', ')', 'g'),
          '\s{2,}', ' ', 'g')
        ORDER BY ord)
    FROM unnest(do_not_overclaim_rules) WITH ORDINALITY AS t(r, ord)
)
WHERE do_not_overclaim_rules IS NOT NULL;

UPDATE profile_assets
SET interview_story   = regexp_replace(coalesce(interview_story,''),
      '(Alyssa Morello|Vianney Santos Bravo|Vianney Santos|Stalin Ochoa|Eric Megargee|John Tomaszewski)', 'a teammate', 'gi'),
    canonical_narrative = regexp_replace(coalesce(canonical_narrative,''),
      '(Alyssa Morello|Vianney Santos Bravo|Vianney Santos|Stalin Ochoa|Eric Megargee|John Tomaszewski)', 'a teammate', 'gi');

\echo ''
\echo '### 1. Da bo ten nguoi ###'


-- =====================================================================
-- 2. PKI OCSP & TRUSTED-CA MITM  (CYB 260)  -> APPROVED
--    Asset manh nhat trong ho so. Bo disclaimer khoi bullet.
-- =====================================================================

UPDATE profile_assets SET
  asset_title = 'PKI OCSP Revocation Reliability & Trusted-CA MITM Analysis',
  resume_bullet_bank = $$- Built a private-CA PKI testbed across three virtual machines (Ubuntu OpenSSL CA with Apache/mod_ssl HTTPS server, Kali Linux running mitmproxy, macOS client) to measure OCSP revocation reliability and trusted-CA interception risk
- Measured revoked-certificate detection time under injected OCSP responder latency (Linux `tc netem`, 0-2000 ms) and identified an unstable soft-fail window near 1950-2000 ms where revocation enforcement collapsed from 100% to 0% within a narrow delay band
- Demonstrated that mitmproxy interception was rejected while the intercepting CA was untrusted, but achieved full TLS visibility once the CA entered the client trust store, quantifying the operational risk of unmanaged trust-store entries$$,
  job_oriented_summary = $$Builds controlled cryptographic and network testbeds, runs latency-injection experiments, and interprets quantitative security-control degradation data. Directly relevant to PKI/TLS, AppSec, and network security roles.$$,
  do_not_overclaim_rules = ARRAY[
    'Do not present academic lab, coursework, or project exposure as professional employment.',
    'Do not claim production deployment, certification, or expert mastery unless separately supported.',
    'The private CA is a self-issued test CA, not a publicly trusted root. Do not imply compromise of a real certificate authority.',
    'The MITM demonstration required voluntarily installing the interception CA into the client trust store. Do not imply rogue certificate issuance or a MITM achievable without administrator action.',
    'Nominally a 4-person course project (CYB 260). If asked directly, acknowledge the team structure and describe own scope.',
    'Re-confirm which dataset a quoted number came from before citing it in an interview; an earlier draft labelled part of the delay sweep as preliminary.',
    'Companion asset: PKI/TLS tool-workflow entry derives from this same project. Do not list both as separate projects.'
  ]::text[],
  status = 'approved',
  updated_at = now()
WHERE id = '80998141-aaef-4fd6-90f2-99711ff101ea';


-- =====================================================================
-- 3. TACH ASSET GOP
--    1297921d gop 2 project: CYB 300 (control implementation)
--                          + CYB 240 (OSSTMM pentest, da co asset rieng)
--    -> 1297921d chi con phan CYB 300.
--    -> a89cdb1c giu phan CYB 240.
--    -> Hai ben cross-reference nhau de giu mach "build roi tu tan cong".
-- =====================================================================

UPDATE profile_assets SET
  asset_title = 'Enterprise Security Control Implementation (NIST SP 800-53 / ISO 27002 / CIS)',
  resume_bullet_bank = $$- Designed and configured a multi-zone enterprise network (Campus / Data Center / DMZ) in GNS3 using Cisco vIOS routers, Arista vEOS switches, and Cisco ASAv firewalls
- Mapped ISO/IEC 27002 and NIST SP 800-53 / 800-41 / 800-63B / 800-92 control objectives to concrete device configuration: AAA/RADIUS authentication, default-deny ACL policy, centralized Syslog, NTP synchronization, and BGP session and route authentication (MD5, TTL-security, route filtering)
- Assessed the firewall configuration against CIS ASA Benchmark v1.1.0 and documented failed hardening controls including missing password-recovery lockout, unenforced SSHv2, excessive session timeouts, and missing NTP authentication$$,
  job_oriented_summary = $$Translates written security-control frameworks into concrete network configuration and self-assesses the result against an industry benchmark. Directly relevant to GRC, security engineering, and network security roles.$$,
  do_not_overclaim_rules = ARRAY[
    'Do not present academic lab, coursework, or project exposure as professional employment.',
    'Do not claim production deployment, certification, or expert mastery unless separately supported.',
    'This is a self-built GNS3 lab with virtual devices only. No real business systems, employees, or customer traffic were involved.',
    'Do not imply a certified, third-party, or client-authorized compliance audit. There was no Rules of Engagement or legal authorization.',
    'Do not claim GNS3 virtual-device configuration as equivalent to hands-on physical network-hardware experience.',
    'Listed as a 2-person course project (CYB 300). Candidate performed the network design, device configuration, control mapping, and CIS benchmark assessment. If asked directly, acknowledge the nominal team structure and describe own scope.',
    'Companion project: the same lab was later attack-validated solo in the OSSTMM CYB 240 project. These are two distinct courses and may be presented as a two-phase build-then-validate narrative, but not as one continuous professional engagement.'
  ]::text[],
  status = 'approved',
  updated_at = now()
WHERE id = '1297921d-8b2e-41be-94a0-4dfea7befe05';


-- =====================================================================
-- 4. OSSTMM ATTACK DETECTION  (CYB 240, SOLO)  -> APPROVED
-- =====================================================================

UPDATE profile_assets SET
  asset_title = 'OSSTMM-Driven Penetration Test & Detection Validation (GNS3 Enterprise Lab)',
  resume_bullet_bank = $$- Conducted a solo OSSTMM 3 (Chapter 11) grey-box penetration test against a self-built GNS3 enterprise network with no initial administrative credentials, using traceroute/ping reconnaissance, Nmap SYN scanning, and tcpdump passive traffic analysis
- Built a documented attack chain: passive VRRP eavesdropping revealed the HA gateway architecture, and a simulated RADIUS-server outage exposed a weak LOCAL fallback credential on a core router, confirmed by dictionary attempt
- Verified that firewall deny events reached the centralized Syslog server, then identified that the logs lacked timestamps and device IDs and were transmitted in plaintext, and proposed Syslog-over-TLS as remediation
- Validated network survivability under simulated component failure, covering VRRP gateway failover and Port-Channel/LACP link redundancy$$,
  job_oriented_summary = $$Independent, methodology-driven penetration testing with attack-chain reasoning and log/forensic-quality assessment. Directly relevant to security analyst, detection engineering, and network security roles.$$,
  do_not_overclaim_rules = ARRAY[
    'Do not present academic lab, coursework, or project exposure as professional employment.',
    'Do not claim production deployment, certification, or expert mastery unless separately supported.',
    'Self-scoped academic lab exercise (CYB 240). Not an authorized, contracted, or client-sanctioned penetration test. No formal Rules of Engagement or legal authorization existed.',
    'Do not claim professional incident-response or production SOC-analyst experience from this project.',
    'Weak credentials were present in a self-built lab by design or incident, not discovered in a real organization live system.',
    'Companion project: the target lab was built in the CYB 300 control-implementation project. Two distinct courses; may be presented as a two-phase build-then-validate narrative.'
  ]::text[],
  status = 'approved',
  updated_at = now()
WHERE id = 'a89cdb1c-dd68-4c77-b3bd-78f53208b06e';


-- =====================================================================
-- 5. DIRTY PIPE  (CYB 200)  -> APPROVED
--    "Reproduced" da ham y khong phai exploit goc -> bo disclaimer o bullet.
-- =====================================================================

UPDATE profile_assets SET
  asset_title = 'Linux Privilege Escalation Analysis: Dirty Pipe (CVE-2022-0847)',
  resume_bullet_bank = $$- Reproduced Dirty Pipe (CVE-2022-0847) root-shell escalation through a SUID binary in a controlled, disposable lab VM, and documented the conditions required for reliable exploitation
- Traced the vulnerability to the kernel/user-mode boundary and the Linux file-permission model, connecting operating-systems theory to a real CVE root cause
- Analyzed privilege-escalation classes (vertical vs. horizontal, SUID misconfiguration, sudo/polkit misuse, kernel-level flaws) and evaluated layered mitigations including kernel patching, SELinux, least privilege, and EDR monitoring$$,
  job_oriented_summary = $$Applies operating-systems theory to interpret a documented CVE and reproduce it under controlled conditions. Relevant as foundational vulnerability-analysis evidence for security analyst and AppSec roles.$$,
  do_not_overclaim_rules = ARRAY[
    'Do not present academic lab, coursework, or project exposure as professional employment.',
    'Do not claim production deployment, certification, or expert mastery unless separately supported.',
    'The demonstration used an existing publicly available proof-of-concept, not an original exploit. Do not claim exploit-development or 0-day-discovery skills.',
    'Run against a disposable purpose-built lab VM, not a real, unknown, or production Linux system.',
    'The exploit reproduces only under specific conditions (matching vulnerable kernel version, no GLIBC mismatch, local access already obtained). Do not imply universal or remote exploit success.',
    'Team course project (CYB 200). If asked directly, acknowledge the team structure and describe own scope.'
  ]::text[],
  status = 'approved',
  updated_at = now()
WHERE id = '9e39468c-144e-4cff-8118-46e59014c37b';


-- =====================================================================
-- 6. LOGISTICS OPTIMIZATION  (CSC 350)  -> APPROVED
--    Chi tiet "3D DP an 32-37GB roi bo" la DIEM MANH: do chu khong doan.
-- =====================================================================

UPDATE profile_assets SET
  asset_title = 'Algorithmic Trade-Off Analysis: Greedy vs. Hybrid DP for Constrained Optimization',
  resume_bullet_bank = $$- Implemented in Python a max-heap greedy heuristic and a two-phase hybrid greedy + dynamic-programming solver for a multi-constraint container-loading (knapsack-style) optimization problem over a cleaned 7,352-item dataset
- Built a memory-efficient sparse dictionary-based 2D DP (Nemhauser-Ullmann style), and empirically established that a full 3D DP formulation was infeasible at scale, exceeding 32-37 GB of RAM even on a reduced 50-item subset
- Quantified greedy vs. hybrid trade-offs across two urgency-weighting configurations, comparing shipment value, weight utilization, and volume utilization$$,
  job_oriented_summary = $$Practical algorithm design and Python implementation with empirical scalability analysis. Relevant to software engineering, algorithm engineering, and data engineering roles.$$,
  do_not_overclaim_rules = ARRAY[
    'Do not present academic lab, coursework, or project exposure as professional employment.',
    'Do not claim production deployment, certification, or expert mastery unless separately supported.',
    'The 7,352-item dataset derives from public Amazon product listings with synthetically assigned urgency labels, not proprietary or real-time logistics data.',
    'The 3D DP was not successfully benchmarked at full scale; it failed on memory exhaustion. Do not claim a completed three-way empirical comparison.',
    'Academic algorithms course project (CSC 350), not a deployed or production supply-chain system.',
    '2-person course project. If asked directly, acknowledge the team structure and describe own scope.'
  ]::text[],
  status = 'approved',
  updated_at = now()
WHERE id = 'd953f372-4eba-42bb-84bc-f3d0c9fa8156';


-- =====================================================================
-- 7. KHONG DUNG TREN CV
--    Giu status='draft' (khong dat gia tri status moi de tranh vi pham
--    CHECK constraint). Danh dau bang rule dau tien.
-- =====================================================================

-- 7a. Computer Architecture x2 -> bo han (m yeu cau)
UPDATE profile_assets SET
  do_not_overclaim_rules = ARRAY[
    'DO NOT USE ON RESUME. Retired 2026-07-28: literature review only, off-target for security roles.',
    'Do not imply hands-on hardware, FPGA, HDL, TPU, or accelerator-design experience.',
    'CircuitVerse is a browser-based educational simulator, not silicon or HDL work.'
  ]::text[],
  updated_at = now()
WHERE id IN ('2351dc8d-2620-4d6c-8e25-7f91b9858d8c',
             'fdbf7683-297b-448b-ae16-328839a8ae83');

-- 7b. LockBit: giu 1 ban, danh dau ban trung
UPDATE profile_assets SET
  do_not_overclaim_rules = ARRAY[
    'DO NOT USE ON RESUME. Interview-only background. Literature review of published threat-intel reports.',
    'Do not imply hands-on malware reverse engineering, binary analysis, debugging, or sandbox detonation.',
    'Do not imply use of IDA Pro, Ghidra, sandboxes, disassemblers, or debuggers.',
    '4-person course project (CSC 340). If asked directly, acknowledge the team structure and describe own scope.',
    'Canonical LockBit asset. The dual-use variant derives from this same paper and is retired.'
  ]::text[],
  updated_at = now()
WHERE id = '2bda4a0f-d43e-4727-a5e9-eeb63a4910c1';

UPDATE profile_assets SET
  do_not_overclaim_rules = ARRAY[
    'DO NOT USE. Retired 2026-07-28: duplicate of the LockBit encryption-architecture asset, same CSC 340 paper.'
  ]::text[],
  updated_at = now()
WHERE id = '610afb6d-081d-42a5-be4f-0b3b724ea589';

-- 7c. PKI/TLS tool workflow -> trung nguon voi asset PKI project
UPDATE profile_assets SET
  do_not_overclaim_rules = ARRAY[
    'DO NOT USE ON RESUME. Retired 2026-07-28: derives from the same CYB 260 project as the PKI OCSP asset.',
    'Tool-level detail retained for interview preparation only (OpenSSL CA setup, OCSP responder config, tc netem).'
  ]::text[],
  updated_at = now()
WHERE id = '419cab20-625b-47fb-ae88-26596b65585c';


-- =====================================================================
-- 8. NGHIEM THU
-- =====================================================================

\echo ''
\echo '### 8a. Trang thai sau khi sua ###'
SELECT status, asset_type, count(*) FROM profile_assets GROUP BY 1,2 ORDER BY 1,2;

\echo ''
\echo '### 8b. 12 asset approved ###'
SELECT asset_type, asset_title, length(resume_bullet_bank) AS len
FROM profile_assets WHERE status='approved' ORDER BY asset_type, asset_title;

\echo ''
\echo '### 8c. Con ten nguoi khong? (KY VONG: 0) ###'
SELECT count(*) AS con_ten_nguoi FROM profile_assets
WHERE (array_to_string(do_not_overclaim_rules,' ') || ' ' ||
       coalesce(interview_story,'') || ' ' || coalesce(canonical_narrative,'') || ' ' ||
       coalesce(resume_bullet_bank,''))
      ~* '(Alyssa|Vianney|Stalin Ochoa|Megargee|Tomaszewski)';

\echo ''
\echo '### 8d. Bullet con chua disclaimer tu phu dinh? (KY VONG: 0) ###'
SELECT asset_title FROM profile_assets
WHERE status='approved'
  AND resume_bullet_bank ~* '(rather than|not a hardware|not evidence of|not original|literature review)';

\echo ''
\echo '### 8e. Rule khong phai cau cam? (KY VONG: 0) ###'
SELECT pa.asset_title, r.rule
FROM profile_assets pa
CROSS JOIN LATERAL unnest(pa.do_not_overclaim_rules) AS r(rule)
WHERE pa.status='approved'
  AND r.rule !~* '^(do not|does not|never|must not|avoid|keep|listed as|nominally|companion|self-scoped|the |this |team |[0-9]-person|re-confirm|weak |academic)';

-- =====================================================================
-- Bo comment dong duoi khi da ung y, roi chay lai file nay.
COMMIT;
-- =====================================================================
ROLLBACK;

\echo ''
\echo '>>> DANG O CHE DO XEM TRUOC (ROLLBACK). Chua ghi gi vao DB. <<<'
\echo '>>> Ung y roi: bo comment dong COMMIT o cuoi file, chay lai.   <<<'
