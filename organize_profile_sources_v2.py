from pathlib import Path
import shutil

SRC = Path("data/profile_raw")
DST = Path("data/profile_sources_v2")

mapping = {
    # Official / identity
    "Transcript.pdf": "00_official/transcript/OFFICIAL__transcript__rider_university__v01.pdf",
    "Transcript Insight.pdf": "03_cross_portfolio_mappings/transcript_insight/PROFILE__academic_trajectory__transcript_insight__v01.pdf",
    "VU PHAN AN NGUYEN-official.pdf": "00_official/resume/OFFICIAL__resume__vu_phan_an_nguyen__v01.pdf",

    # Project profiles synthesized from your projects
    "Cyb260 Project Pki Ocsp Mitm Profile.pdf": "02_project_profiles/CYB260_pki_ocsp_mitm/CYB260__project_profile__pki_ocsp_trusted_ca_mitm__v01.pdf",
    "Csc340 Project Lockbit Profile.pdf": "02_project_profiles/CSC340_lockbit_analysis/CSC340__project_profile__lockbit_malware_analysis__v01.pdf",
    "Csc 350 Project Logistics Optimization Profile.pdf": "02_project_profiles/CSC350_logistics_optimization/CSC350__project_profile__logistics_optimization__v01.pdf",
    "Cyb200 Project Privilege Escalation Profile.pdf": "02_project_profiles/CYB200_dirty_pipe_privilege_escalation/CYB200__project_profile__dirty_pipe_privilege_escalation__v01.pdf",
    "Cyb240 Project Attack Detection Profile.pdf": "02_project_profiles/CYB240_attack_detection/CYB240__project_profile__attack_detection__v01.pdf",
    "Cyb300 Project Nist Iso Enterprise Profile.pdf": "02_project_profiles/CYB300_nist_iso_enterprise_security/CYB300__project_profile__nist_iso_enterprise_security__v01.pdf",
    "Cyb300 Cyb240 Project Mapping.pdf": "02_project_profiles/CYB300_nist_iso_enterprise_security/CYB300_CYB240__project_mapping__enterprise_security_attack_detection__v01.pdf",
    "Csc 220 Computer Architecture Project Profile.pdf": "02_project_profiles/CSC220_computer_architecture_project/CSC220__project_profile__computer_architecture_alu_modeling__v01.pdf",
    "CSC220_Project_Paper (1).pdf": "02_project_profiles/CSC220_computer_architecture_project/CSC220__project_paper__computer_architecture__v01.pdf",
    "CSC340_Implementation_Details.pdf": "02_project_profiles/CSC340_lockbit_analysis/CSC340__implementation_details__lockbit_analysis__v01.pdf",
    "Causal_Influence_Graph_with_Adaptive_Mean_Field.pdf": "02_project_profiles/CYB490_cig_amf_ai_research/CYB490__research_paper__cig_amf__v01.pdf",
    "Cyb 490 Cig-amf Ai Research Project Profile.pdf": "02_project_profiles/CYB490_cig_amf_ai_research/CYB490__research_profile__cig_amf_ai_research__v01.pdf",

    # Course profiles synthesized per course
    "Cyb300 Revised Full Profile With New Sources.docx": "01_course_profiles/CYB300_cybersecurity_programs/CYB300__course_profile__cybersecurity_programs_revised__v02.docx",
    "Cyb260 Network Defense Countermeasures Profile.docx": "01_course_profiles/CYB260_network_defenses/CYB260__course_profile__network_defenses_countermeasures__v01.docx",
    "Cyb200 Data Enrichment Profile.docx": "01_course_profiles/CYB200_operating_systems_security/CYB200__course_profile__operating_systems_security__v01.docx",
    "Cyb130 Short Data Enrichment Profile.docx": "01_course_profiles/CYB130_cybersecurity_essentials/CYB130__course_profile__cybersecurity_essentials__v01.docx",
    "Cyb110 Short Data Enrichment Profile.docx": "01_course_profiles/CYB110_cyber_ethics/CYB110__course_profile__cyber_ethics__v01.docx",
    "Csc260 Data Enrichment Profile.docx": "01_course_profiles/CSC260_computer_networks/CSC260__course_profile__computer_networks__v01.docx",
    "Csc250 Secure Software Web Security Profile.docx": "01_course_profiles/CSC250_secure_software_web_security/CSC250__course_profile__secure_software_web_security__v01.docx",
    "Csc230 Data Enrichment Profile.docx": "01_course_profiles/CSC230_probability_cs/CSC230__course_profile__probability_for_computer_scientists__v01.docx",
    "Csc220 Detailed Data Enrichment Profile Chapters 1 To 8.docx": "01_course_profiles/CSC220_computer_architecture/CSC220__course_profile__computer_architecture_chapters_1_to_8__v01.docx",
    "Csc200 Data Enrichment Profile.docx": "01_course_profiles/CSC200_data_structures_algorithms/CSC200__course_profile__data_structures_algorithms__v01.docx",
    "Csc140 Rewritten Data Enrichment Profile.docx": "01_course_profiles/CSC140_discrete_structures/CSC140__course_profile__discrete_structures__v01.docx",
    "Csc130 Data Enrichment Profile V2.docx": "01_course_profiles/CSC130_computer_science_ii/CSC130__course_profile__computer_science_ii__v02.docx",
    "Csc110 Complete Data Enrichment Profile.docx": "01_course_profiles/CSC110_computer_science_i/CSC110__course_profile__computer_science_i__v01.docx",
    "Csc 350 Analysis Of Algorithms Course Profile.docx": "01_course_profiles/CSC350_analysis_of_algorithms/CSC350__course_profile__analysis_of_algorithms__v01.docx",
    "Csc 260 Computer Networks Foundation Portfolio Profile.docx": "01_course_profiles/CSC260_computer_networks/CSC260__course_profile__computer_networks_foundation_portfolio__v01.docx",
    "Cis330 Database Systems Data Enrichment Profile.docx": "01_course_profiles/CIS330_database_systems/CIS330__course_profile__database_systems__v01.docx",
    "Cyber War Course Strategic Profile.docx": "01_course_profiles/CYBERWAR_strategic_cyber_conflict/CYBERWAR__course_profile__strategic_cyber_conflict__v01.docx",
    "Cyb320 Lecture Class Digital Forensics Profile.docx": "01_course_profiles/CYB320_digital_forensics/CYB320__course_profile__digital_forensics_lecture__v01.docx",
    "Cyb320 Forensics Lab Series Profile.docx": "01_course_profiles/CYB320_digital_forensics/CYB320__lab_series_profile__digital_forensics__v01.docx",

    # Original source papers / course readings, not direct profile truth
    "CSC_350 (2).pdf": "04_source_papers_and_course_readings/CSC350_analysis_of_algorithms/CSC350__source_paper__analysis_of_algorithms__v01.pdf",
    "CYB_240 (7).pdf": "04_source_papers_and_course_readings/CYB240_ethical_hacking_pentesting/CYB240__source_paper__ethical_hacking_pentesting__v01.pdf",
    "CYB_260.pdf": "04_source_papers_and_course_readings/CYB260_network_defenses/CYB260__source_paper__network_defenses__v01.pdf",
    "CYB_300 (5).pdf": "04_source_papers_and_course_readings/CYB300_cybersecurity_programs/CYB300__source_paper__cybersecurity_programs__v01.pdf",

    # Cross-portfolio mappings
    "Cyber Tools Frameworks Source Mapping.pdf": "03_cross_portfolio_mappings/tools_frameworks/PROFILE__tool_workflow_mapping__cybersecurity_tools_frameworks__v01.pdf",
    "Tools.docx": "03_cross_portfolio_mappings/tools_frameworks/PROFILE__tool_inventory__tools_workflows__v01.docx",

    # Guidance, not profile truth
    "Calculus Qualifications Portfolio Mapping.docx": "05_guidance_not_truth/qualifications_certifications/GUIDANCE__portfolio_mapping__calculus_qualifications__v01.docx",
    "Báo cáo nghiên cứu sâu về qualifications, tools, skills và certifications cho sinh viên cử nhân Comp.pdf": "05_guidance_not_truth/qualifications_certifications/GUIDANCE__research_report__qualifications_tools_skills_certifications__v01.pdf",

    # Bundles / cross references
    "TỔNG HỢP.pdf": "99_source_bundles/tong_hop/PORTFOLIO__source_bundle__tong_hop__v01.pdf",
    "TỔNG HỢP_CÁC PROJECT 2.pdf": "99_source_bundles/tong_hop/PORTFOLIO__source_bundle__projects_tong_hop__v02.pdf",
}

DST.mkdir(parents=True, exist_ok=True)

copied = 0
missing = []
unmapped = []

for old, new in mapping.items():
    src = SRC / old
    dst = DST / new
    if not src.exists():
        missing.append(old)
        continue
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    copied += 1

for p in SRC.iterdir():
    if p.is_file() and not p.name.startswith(".") and p.name not in mapping:
        unmapped.append(p.name)

print(f"Copied: {copied}")
print("")
print("Missing from source or mapping:")
for x in missing:
    print(f"- {x}")

print("")
print("Unmapped existing files:")
for x in unmapped:
    print(f"- {x}")

print("")
print(f"Output: {DST}")
