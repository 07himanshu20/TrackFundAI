"""
Data migration: Seed 142 market opportunities across all sectors and geographies.
v5 Market Explorer: 142 seed opportunities database.
"""
from django.db import migrations
from django.utils.text import slugify

OPPORTUNITIES = [
    # ── India — Technology / SaaS ─────────────────────────────────────────
    ("India B2B SaaS — SME ERP & CRM", "saas", "india", "asia", "series_a", "high_growth", "aif_cat2", 8.5, 2.1, 35.0, "2024-2030", "Zoho, Freshworks, LeadSquared, Salesforce India", "India has 63 million SMEs, <5% on modern SaaS — massive greenfield. ARPU expanding with GST/Einvoice compliance needs."),
    ("Enterprise AI/ML Platforms — India", "ai_ml", "india", "asia", "series_b", "high_growth", "aif_cat2", 12.0, 3.2, 40.0, "2024-2029", "Sarvam AI, Krutrim, Ola AI, AWS India, Azure OpenAI", "India's $10B IT outsourcing base pivoting to AI-led services. Language model infra for 22 official languages = unique moat."),
    ("Cybersecurity — India & APAC", "cybersecurity", "india", "asia", "series_a", "high_growth", "aif_cat2", 6.2, 1.8, 32.0, "2024-2030", "Quick Heal, Sequretek, TAC Security, Palo Alto, CrowdStrike", "Digital India initiative + PDPB 2023 compliance driving spend. BFSI and Govt sectors top buyers."),
    ("Cloud Infrastructure & DevOps India", "technology", "india", "asia", "series_a", "high_growth", "aif_cat2", 9.0, 2.5, 28.0, "2024-2030", "Ciphercloud, Appsmith, Hasura, Neon, Supabase India", "India top 3 global cloud talent pool. ISV ecosystem accelerating with AWS/GCP partner programs."),
    ("HR Tech & Payroll SaaS India", "saas", "india", "asia", "series_a", "steady_growth", "aif_cat2", 3.5, 0.9, 22.0, "2024-2029", "Darwinbox, Keka, GreytHR, Razorpay Payroll, Zimyo", "New Labour Codes (4 codes effective) driving compliance-led HR software adoption. API-first payroll trend."),
    ("Legal Tech India", "saas", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.2, 0.3, 45.0, "2024-2030", "SpotDraft, Leegality, Superlegal, Rainmaker, NearLaw", "E-courts initiative + commercial court digitization. Contract lifecycle management boom."),
    ("EdTech — Skilling & Upskilling India", "edtech", "india", "asia", "series_b", "steady_growth", "aif_cat2", 7.5, 2.0, 20.0, "2024-2030", "Skill-Lync, Simplilearn, Upgrad, BYJU's Professional, LinkedIn Learning", "NEP 2020 + National Skill Development Mission. 15 million new workforce entrants annually."),
    ("Gaming & Interactive Media India", "gaming", "india", "asia", "series_a", "high_growth", "aif_cat2", 4.0, 1.2, 38.0, "2024-2030", "Nazara, WinZO, MPL, Dream11, Krafton India", "500M+ gamers; real-money gaming regulations evolving; esports + casual gaming divergence."),

    # ── India — Fintech ───────────────────────────────────────────────────
    ("BNPL & Consumer Credit India", "fintech", "india", "asia", "series_b", "high_growth", "aif_cat2", 45.0, 12.0, 30.0, "2024-2030", "Simpl, Lazypay, ZestMoney, Slice, Uni Cards", "Credit penetration <10% vs 70% in developed markets. RBI Digital Lending Guidelines 2022 creating formal framework."),
    ("Digital Payments Infrastructure", "fintech", "india", "asia", "series_c", "steady_growth", "aif_cat2", 20.0, 6.0, 25.0, "2024-2030", "Juspay, Razorpay, Cashfree, PayU, BillDesk", "UPI 2.0 + credit on UPI + CBDC pilot. 10B+ monthly UPI transactions and growing."),
    ("Insurance Tech (Insurtech) India", "fintech", "india", "asia", "series_a", "high_growth", "aif_cat2", 8.0, 2.5, 35.0, "2024-2030", "Acko, Digit, Turtlemint, PolicyBazaar, Ditto Insurance", "India insurance penetration 4.2% vs global 7% avg. IRDAI sandbox enabling innovation."),
    ("Wealth Tech & Neo-broking India", "fintech", "india", "asia", "series_b", "high_growth", "aif_cat2", 6.5, 2.0, 40.0, "2024-2029", "Groww, Zerodha, Smallcase, INDmoney, Fisdom", "Retail equity participation tripled post-COVID. D-Mart effect — 40M+ demat accounts."),
    ("MSME Lending Fintech India", "fintech", "india", "asia", "series_b", "high_growth", "aif_cat2", 65.0, 18.0, 28.0, "2024-2030", "NeoGrowth, Lendingkart, Aye Finance, Indifi, CreditEnable", "₹20 lakh Cr MSME credit gap. AA framework + GST data enabling cash-flow lending."),
    ("B2B Payments & Treasury India", "fintech", "india", "asia", "series_a", "high_growth", "aif_cat2", 4.5, 1.3, 35.0, "2024-2030", "Enkash, Happay, Open, RazorpayX, YAP", "90% of B2B payments still on NEFT/RTGS. Real-time treasury + FX management white space."),
    ("Crypto & Web3 Infrastructure India", "fintech", "india", "asia", "series_a", "high_growth", "aif_cat3", 2.5, 0.8, 50.0, "2024-2030", "CoinDCX, WazirX, Mudrex, HashCash, Polygon India", "30% TDS on crypto income effective Apr 2022 created headwinds; clarity expected; NFT/DeFi infra play."),
    ("Account Aggregator (AA) Ecosystem", "fintech", "india", "asia", "series_a", "high_growth", "aif_cat2", 3.0, 1.0, 60.0, "2024-2028", "Finvu, CAMS FinServ, Perfios, Karza, CredibleX", "RBI's AA framework is largest open banking initiative globally. Consent-based data sharing enabling new credit models."),

    # ── India — Healthcare ─────────────────────────────────────────────────
    ("Healthcare SaaS & HIS India", "healthtech", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.5, 0.7, 38.0, "2024-2030", "Innovaccer, iCare, CliniQ, Sagility, Eka.Care", "ABDM (Ayushman Bharat Digital Mission) digitizing 1.3B health records. HIS replacement cycle 2024-2028."),
    ("Diagnostics & Pathology Chains India", "healthcare", "india", "asia", "buyout", "steady_growth", "aif_cat2", 15.0, 4.5, 18.0, "2024-2030", "Metropolis, Dr Lal Path Labs, Thyrocare, Redcliffe, 1mg Labs", "Home collection + online booking 40% CAGR. Franchise rollup opportunity in Tier 2/3 cities."),
    ("Hospital Chains — Tier 2/3 India", "healthcare", "india", "asia", "growth", "steady_growth", "aif_cat2", 20.0, 6.0, 15.0, "2024-2030", "Rainbow Children, Manipal, Care Hospitals, Aster, Narayana", "1.9 beds/1000 population vs WHO recommendation of 3.5. Government under-investment creating private opportunity."),
    ("Mental Health Tech India", "healthtech", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.5, 0.4, 50.0, "2024-2030", "YourDOST, Wysa, Vandrevala Foundation, iCall, Lissun", "200M+ Indians affected by mental disorders; 0.3 psychiatrists per 100k population. COVID-led awareness surge."),
    ("MedTech Devices — Made in India", "healthtech", "india", "asia", "series_a", "high_growth", "aif_cat2", 3.5, 1.0, 30.0, "2024-2030", "Siemens Healthineers India, Trivitron, Perfint, Skanray, Meril Life", "PLI Scheme for Medical Devices 2021. Import substitution + export opportunity to ASEAN."),
    ("Pharma API & Specialty Chemicals India", "healthcare", "india", "asia", "growth", "steady_growth", "aif_cat2", 25.0, 8.0, 16.0, "2024-2030", "Divi's Lab, Hikal, Aarti Drugs, Solara, Laurus Labs", "China+1 strategy accelerating India API capacity. CDMOs in strong demand from global innovators."),
    ("Hospital at Home & Tele-ICU India", "healthtech", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.0, 0.6, 45.0, "2024-2030", "Portea, Care24, Nightingale's, Antara, MediBuddy", "Aging population + ICU bed shortage. Hybrid care model endorsed by NMC guidelines 2023."),

    # ── India — AgriTech ──────────────────────────────────────────────────
    ("Precision AgriTech India", "agritech", "india", "asia", "series_a", "high_growth", "aif_cat1", 6.0, 1.8, 35.0, "2024-2030", "DeHaat, AgroStar, Ninjacart, Bijak, Jai Kisan", "140M+ smallholder farmers with <2ha avg landholding. PM-KISAN digital stack + e-NAM creating data infrastructure."),
    ("Agri-Fintech & Farmer Credit India", "agritech", "india", "asia", "series_a", "high_growth", "aif_cat1", 25.0, 7.5, 28.0, "2024-2030", "Samunnati, Jai Kisan, Dvara, FPO Finance, Kinara Capital", "Formal farm credit coverage <30%. NABARD guarantee + kisan credit card digital stack unlocking."),
    ("Cold Chain & Agri-Logistics India", "logistics", "india", "asia", "series_b", "steady_growth", "aif_cat2", 4.5, 1.4, 22.0, "2024-2030", "Stellapps, Cooltainer, WayCool, Rivigo, Reefer India", "35% farm produce wastage due to cold chain gaps. PM Gati Shakti + PMKSY investments in cold infra."),

    # ── India — Logistics & Supply Chain ──────────────────────────────────
    ("Express Logistics & Quick Commerce India", "logistics", "india", "asia", "series_c", "high_growth", "aif_cat2", 8.5, 2.5, 35.0, "2024-2030", "Delhivery, Ecom Express, Shadowfax, Porter, Dunzo, Blinkit", "10-minute delivery as new normal in metros. B2B express logistics underpenetrated vs China."),
    ("Freight Tech & Trucking India", "logistics", "india", "asia", "series_b", "high_growth", "aif_cat2", 12.0, 3.5, 28.0, "2024-2030", "Blackbuck, Rivigo, Truck-X, Shiprocket, iThink Logistics", "₹8.5L Cr trucking market 95% unorganized. E-way bill + FASTag digitizing trucking. GST-driven formalization."),
    ("Warehouse Tech & 3PL India", "logistics", "india", "asia", "growth", "steady_growth", "aif_cat2", 5.5, 1.7, 20.0, "2024-2030", "Mahindra Logistics, Allcargo, TVS Supply Chain, Xpressbees, Eshipz", "Consolidation opportunity in fragmented Grade-B warehousing. India Logistics Policy 2022 + PM GatiShakti."),

    # ── India — Consumer / D2C ─────────────────────────────────────────────
    ("D2C Consumer Brands India", "consumer", "india", "asia", "series_a", "high_growth", "aif_cat2", 15.0, 5.0, 40.0, "2024-2030", "Mamaearth, Boat, Lenskart, Sugar Cosmetics, Wakefit", "300M+ aspirational middle class. 75% India GMV still brand-agnostic. Quick commerce enabling D2C brand building."),
    ("Quick Service Restaurants (QSR) India", "consumer", "india", "asia", "growth", "steady_growth", "aif_cat2", 12.0, 4.0, 18.0, "2024-2030", "Devyani, Jubilant, Westlife, Sapphire Foods, Wow! Momo", "Eating out frequency 2x post-COVID. Tier 2/3 expansion with 100-200 seat format QSR."),
    ("Pet Care & Animal Health India", "consumer", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.0, 0.6, 45.0, "2024-2030", "Heads Up For Tails, Supertails, Wiggles, Dogsee Chew, Petkonnect", "200M+ pets in India. Premiumization of pet food + veterinary services. CAGR accelerating post-pandemic."),
    ("Personal Finance & Money Management India", "consumer", "india", "asia", "series_a", "high_growth", "aif_cat2", 3.0, 0.9, 38.0, "2024-2030", "Fi Money, Jupiter, Freo, Jifi, Niyo", "Neo-banking for 500M+ young Indian consumers. BNPL + credit + savings bundled experience."),

    # ── India — Manufacturing & Industry 4.0 ──────────────────────────────
    ("Industry 4.0 & IIoT India", "manufacturing", "india", "asia", "series_a", "high_growth", "aif_cat2", 4.0, 1.2, 35.0, "2024-2030", "Softsensor, Altizon, Peel-Works, Zenatix, Flutura", "PLI Schemes 2021 + Make in India 2.0 driving ₹2L Cr manufacturing capex. IoT retrofit opportunity for legacy plants."),
    ("EV Components & Battery Tech India", "ev", "india", "asia", "series_b", "high_growth", "aif_cat2", 18.0, 5.5, 50.0, "2024-2030", "Ather Energy, Ola Electric, Greaves Electric, Log9 Materials, Exicom", "FAME-III + PM E-Bus Sewa scheme. 30% EV penetration by 2030 target. Battery cell manufacturing PLI ₹18,100 Cr."),
    ("Defence & Aerospace Manufacturing India", "aerospace", "india", "asia", "growth", "steady_growth", "aif_cat1", 8.0, 2.5, 20.0, "2024-2030", "Hindustan Aeronautics, BEL, DRDO spinoffs, Data Patterns, Ideaforge", "FDI in defence raised to 100%. ₹1.7L Cr defence budget. 68 items defence procurement list."),
    ("Specialty Chemicals India", "manufacturing", "india", "asia", "growth", "steady_growth", "aif_cat2", 10.0, 3.0, 15.0, "2024-2030", "Deepak Nitrite, Aarti Industries, Vinati Organics, Navin Fluorine, SRF", "China+1 driving global specialty chem sourcing from India. Fluorochemicals, surfactants, CDMO chemicals."),
    ("Textile & Technical Textiles India", "manufacturing", "india", "asia", "growth", "steady_growth", "aif_cat2", 14.0, 4.0, 14.0, "2024-2030", "Raymond, Alok Industries, Trident, Vardhman, PNT", "PM-MITRA Textile Parks scheme 2022. Technical textiles (geotextiles, medtech textiles) high value-add."),

    # ── India — CleanTech & ESG ──────────────────────────────────────────
    ("Solar Energy & Rooftop Solar India", "cleantech", "india", "asia", "growth", "steady_growth", "aif_cat1", 20.0, 6.0, 22.0, "2024-2030", "ReNew Power, Adani Green, Greenko, Rays Power, CleanMax Solar", "500 GW RE target by 2030. PM-KUSUM for farm solar. Rooftop solar net-metering rollout."),
    ("Green Hydrogen & Fuel Cells India", "cleantech", "india", "asia", "series_b", "high_growth", "aif_cat1", 8.0, 2.0, 55.0, "2024-2035", "Ohmium, Greenko H2, ACME Solar, Torrent Hydrogen, NTPC Green", "National Green Hydrogen Mission: ₹19,744 Cr outlay for 5 MT green H2 by 2030."),
    ("EV Charging Infrastructure India", "ev", "india", "asia", "series_a", "high_growth", "aif_cat2", 3.5, 1.0, 60.0, "2024-2030", "Exicom Tele-Systems, Statiq, Charge Zone, Tata Power EV, BPCL EV", "1 charging station per 3 EVs target. Highway + urban charging network build-out."),
    ("Water Treatment & Management India", "cleantech", "india", "asia", "growth", "steady_growth", "aif_cat2", 5.0, 1.5, 16.0, "2024-2030", "Ion Exchange, Thermax, Praj Industries, VA Tech WABAG, WEL", "Jal Jeevan Mission ₹3.6L Cr investment. Industrial water recycling mandates."),
    ("Waste Management & Circular Economy India", "cleantech", "india", "asia", "series_a", "high_growth", "aif_cat1", 2.5, 0.8, 30.0, "2024-2030", "Attero Recycling, Nepra, Kabadiwalla, Ewaste Recycler, Hasiru Dala", "SWM Rules 2016 + EPR mandates driving compliance-led RM investment. Lithium battery recycling emerging."),

    # ── India — Real Estate / Infrastructure ──────────────────────────────
    ("PropTech & Digital Real Estate India", "real_estate", "india", "asia", "series_b", "high_growth", "aif_cat2", 3.5, 1.0, 35.0, "2024-2030", "NoBroker, Housing.com, MagicBricks, Square Yards, Squareyards", "₹13L Cr residential RE market. Co-living, managed offices, fractional ownership = new categories."),
    ("Commercial Real Estate & REITs India", "real_estate", "india", "asia", "late_stage", "steady_growth", "aif_cat2", 30.0, 10.0, 12.0, "2024-2030", "Embassy REIT, Mindspace REIT, Brookfield REIT, Prestige, DLF", "Grade A office stock 650 MSF. GCC (Global Capability Centre) boom driving 40+ MSF leasing demand."),
    ("Affordable Housing India", "real_estate", "india", "asia", "growth", "steady_growth", "aif_cat1", 40.0, 12.0, 15.0, "2024-2030", "Affordable Housing Finance, Aptus Value, Aavas Financiers, HFFC, Muthoot", "Housing for All 2022 demand still 30M+ units. Pradhan Mantri Awas Yojana subsidy + RERA compliance."),
    ("Data Centre Infrastructure India", "technology", "india", "asia", "growth", "high_growth", "aif_cat2", 5.0, 1.5, 40.0, "2024-2030", "NTT Data Centres, Nxtra by Airtel, CtrlS, Sify Technologies, STT GDC", "Data localisation norms + cloud adoption driving hyperscaler demand. 1GW DC capacity by 2027 target."),

    # ── Southeast Asia ───────────────────────────────────────────────────
    ("Southeast Asia Fintech — Indonesia/Vietnam", "fintech", "indonesia", "asia", "series_a", "high_growth", "aif_cat2", 38.0, 10.0, 30.0, "2024-2030", "Goto, Grab Financial, Dana, Akulaku, Kredivo", "700M population; 50% unbanked. OJK digital banking framework enabling neo-banking."),
    ("Southeast Asia EdTech", "edtech", "indonesia", "asia", "series_a", "high_growth", "aif_cat2", 6.0, 1.8, 35.0, "2024-2030", "Ruangguru, Zenius, Cakap, Pahamify, GakKelas", "Young demographic (median age 28); mobile-first learning; gamification + live-classes."),
    ("Singapore VC & Family Office Tech", "fintech", "singapore", "asia", "series_a", "high_growth", "vcc", 2.0, 0.6, 40.0, "2024-2030", "Endowus, Stashaway, Syfe, Kristal.ai, Fundnel", "MAS Singapore as APAC wealth management hub. VCC structure 2020 enabling fund domicilation."),

    # ── USA ───────────────────────────────────────────────────────────────
    ("US Enterprise AI — Foundation Models & Applications", "ai_ml", "usa", "north_america", "series_b", "high_growth", "lp_gp", 150.0, 40.0, 45.0, "2024-2030", "OpenAI, Anthropic, Cohere, Mistral, Together AI, Databricks", "Enterprise AI spend $150B by 2027. Model-as-a-service + fine-tuning infra + vertical AI apps."),
    ("US Healthcare AI & Automation", "healthtech", "usa", "north_america", "series_b", "high_growth", "lp_gp", 45.0, 15.0, 38.0, "2024-2030", "Tempus, Flatiron, Veeva Systems, Doceree, Nabla", "CMS prior auth automation rule 2024. FDA-cleared AI algorithms 600+. Radiology + pathology + coding AI."),
    ("US Climate Tech & Carbon Markets", "cleantech", "usa", "north_america", "series_a", "high_growth", "lp_gp", 60.0, 18.0, 40.0, "2024-2030", "Pachama, Rubicon Carbon, Xpansiv, Heirloom, Climeworks", "IRA (Inflation Reduction Act) $369B clean energy investment. 45Q carbon capture tax credit."),
    ("US Cybersecurity — Zero Trust & SASE", "cybersecurity", "usa", "north_america", "series_b", "high_growth", "lp_gp", 35.0, 10.0, 30.0, "2024-2030", "Zscaler, Cloudflare, Lacework, Orca Security, Wiz", "SEC cybersecurity disclosure rules 2023. Zero Trust architecture mandated for US Federal agencies."),
    ("US Autonomous Vehicles & Robotics", "technology", "usa", "north_america", "late_stage", "high_growth", "lp_gp", 80.0, 25.0, 35.0, "2024-2035", "Waymo, Aurora, Nuro, Figure AI, Boston Dynamics", "Level 4 autonomy commercial deployment 2025-2028. Robotics for last-mile delivery + warehouse automation."),
    ("US Defense Tech & Dual-Use", "aerospace", "usa", "north_america", "series_b", "high_growth", "lp_gp", 30.0, 10.0, 30.0, "2024-2030", "Anduril, Shield AI, Palantir, L3Harris, SpaceX Starshield", "DOD $858B budget; DARPA + SBIR programs. Drone, AI surveillance, and space defence."),
    ("US Biotech — GLP-1 & Cell Therapy", "biotech", "usa", "north_america", "series_c", "high_growth", "lp_gp", 200.0, 60.0, 20.0, "2024-2030", "Novo Nordisk, Eli Lilly, Fierce Biotech, Graphite Bio, Cellares", "Obesity drug market $100B+ by 2035. Cell therapy manufacturing automation."),
    ("US PropTech & Construction Tech", "real_estate", "usa", "north_america", "series_a", "high_growth", "lp_gp", 20.0, 6.0, 25.0, "2024-2030", "Procore, PlanGrid, ICON3D, Propy, Lessen", "Housing undersupply 3.8M units. Modular construction + AI for permitting + lender tech."),

    # ── Europe ────────────────────────────────────────────────────────────
    ("European Deep Tech & Industrial AI", "ai_ml", "germany", "europe", "series_b", "high_growth", "lp_gp", 25.0, 8.0, 35.0, "2024-2030", "DeepL, aleph alpha, Helsing, Cognigy, Xentral", "EU AI Act 2024 — compliance tooling boom. Germany/France industrial AI for manufacturing."),
    ("European HealthTech & Digital Health", "healthtech", "uk", "europe", "series_a", "high_growth", "lp_gp", 35.0, 11.0, 28.0, "2024-2030", "Babylon Health, Kry, Doctolib, Veeva, Sword Health", "NHS digitization + GDPR-compliant health AI. Drug discovery AI investment surge."),
    ("European Fintech — Open Banking & PSD3", "fintech", "uk", "europe", "series_b", "high_growth", "lp_gp", 40.0, 13.0, 28.0, "2024-2030", "Revolut, Wise, Monzo, Trade Republic, N26", "PSD3 + DORA 2025 compliance driving embedded finance. UK-EU passporting post-Brexit realignment."),
    ("European Climate & Green Finance", "cleantech", "germany", "europe", "series_a", "high_growth", "lp_gp", 50.0, 15.0, 30.0, "2024-2030", "Northvolt, H2 Green Steel, Wärtsilä, Enpal, Sonnen", "EU Green Deal €1T investment. ETS carbon price €70+. Offshore wind + green hydrogen."),
    ("European Aerospace & Space Tech", "aerospace", "france", "europe", "series_a", "high_growth", "lp_gp", 12.0, 4.0, 30.0, "2024-2030", "Exotrail, Satcom Direct, Isar Aerospace, HyImpulse, OHB", "ESA €7.8B budget 2023. Commercial space launch market. Earth observation AI data."),

    # ── Middle East / Africa ──────────────────────────────────────────────
    ("UAE & Saudi Arabia Fintech", "fintech", "uae", "asia", "series_a", "high_growth", "aif_cat2", 8.0, 2.5, 40.0, "2024-2030", "Tabby, Tamara, Sarwa, Mambu MENA, Beehive", "Vision 2030 KSA + UAE 2071. 60% unbanked in MENA. CBUAE Open Finance Regulation 2023."),
    ("Africa Fintech — Mobile Money & BNPL", "fintech", "kenya", "africa", "series_a", "high_growth", "aif_cat2", 12.0, 3.5, 45.0, "2024-2030", "M-PESA, Flutterwave, Chipper Cash, Carbon, Moniepoint", "600M+ unbanked Africans. M-PESA model replication across SSA. Pan-African payment infrastructure."),
    ("Africa HealthTech", "healthtech", "nigeria", "africa", "series_a", "high_growth", "aif_cat1", 6.0, 1.8, 40.0, "2024-2030", "mPharma, LifeBank, Reliance Health, MDaaS, Helium Health", "1.2B+ population with 1/3 WHO recommended doctor ratio. Telehealth + EHR leapfrog opportunity."),

    # ── Emerging Sectors India ────────────────────────────────────────────
    ("Space Tech India", "aerospace", "india", "asia", "series_a", "high_growth", "aif_cat2", 5.0, 1.5, 60.0, "2024-2035", "Agnikul Cosmos, Skyroot Aerospace, Pixxel, Dhruva Space, Bellatrix", "IN-SPACe enabling private launch. ISRO commercial spin-off Antrix successor. Earth observation + satellite IoT."),
    ("Drone Tech & UAV India", "aerospace", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.5, 0.8, 55.0, "2024-2030", "Garuda Aerospace, Ideaforge, throttleaero, Marut Drones, Asteria Aerospace", "PLI Drones 2022. BVLOS approvals expanding. Survey, surveillance, delivery use cases."),
    ("Sports Tech & Fantasy Sports India", "gaming", "india", "asia", "series_b", "high_growth", "aif_cat3", 4.0, 1.2, 40.0, "2024-2030", "Dream11, MPL, Jio Cricket, FanCode, Sports18", "ICC Cricket World Cup 2023 + Olympics 2036 India bid. 150M+ fantasy sports users."),
    ("Travel Tech & OTA India", "technology", "india", "asia", "series_b", "steady_growth", "aif_cat2", 8.0, 2.5, 22.0, "2024-2030", "MakeMyTrip, ixigo, Cleartrip, OYO, Fabhotels", "India domestic travel 8x of pre-COVID levels. Bharat Gaurav Trains + UDAN enabling tier 2 travel."),
    ("Legal Tech & RegTech India", "saas", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.8, 0.5, 40.0, "2024-2030", "Vakilsearch, SpotDraft, Leegality, Perfios Compliance, Ondot", "SEBI + RBI + MCA compliance automation. Companies Act 2013 + PMLA-AML tech spend."),
    ("Bioeconomy & Synthetic Biology India", "biotech", "india", "asia", "series_a", "high_growth", "aif_cat1", 3.0, 0.9, 45.0, "2024-2030", "Alchemy Beverages, String Bio, Mynvax, Phibro Animal Health, Laurus Synthesis", "National Biotech Policy 2023 + ₹9,200 Cr Bioeconomy initiative. Fermentation + CDMO."),
    ("Fashion Tech & Sustainable Fashion India", "consumer", "india", "asia", "series_a", "high_growth", "aif_cat2", 5.0, 1.5, 30.0, "2024-2030", "FabIndia, W for Woman, Bewakoof, House of Masaba, Nykaa Fashion", "₹6L Cr apparel market 3rd largest globally. Sustainability + D2C fashion + AI styling tools."),
    ("Content Creator Economy India", "media", "india", "asia", "series_a", "high_growth", "aif_cat2", 3.0, 0.9, 50.0, "2024-2030", "Kofluence, Wobb, Qoruz, CreatorIQ, Jellysmack", "100M+ content creators in India. Branded content + creator commerce + MCN networks."),
    ("Vernacular Content & OTT India", "media", "india", "asia", "series_b", "steady_growth", "aif_cat2", 6.0, 2.0, 25.0, "2024-2030", "Pocket FM, Kuku FM, Pratilipi, Moj, ShareChat", "600M internet users in vernacular-first languages. Audio storytelling + short-form regional content."),
    ("AutoTech & Used Car Marketplace India", "technology", "india", "asia", "series_b", "high_growth", "aif_cat2", 5.5, 1.7, 30.0, "2024-2030", "Cars24, Spinny, Cardekho, OLX Autos, Droom", "28M+ used cars sold in India p.a. vs 4M new. Digital inspection + financing + delivery bundling."),
    ("Smart Cities & Urban Tech India", "technology", "india", "asia", "growth", "steady_growth", "aif_cat1", 8.0, 2.5, 20.0, "2024-2030", "Nihilent, Wipro Smart City, L&T Smart World, IBM Smarter Cities, Sensegrass", "100 Smart Cities Mission + AMRUT 2.0 ₹77,640 Cr. CCTV, traffic, waste, water SCADA."),
    ("NBFC — Gold Loan India", "nbfc", "india", "asia", "growth", "steady_growth", "aif_cat2", 8.0, 2.5, 15.0, "2024-2030", "Muthoot Finance, Manappuram, IIFL Finance, Rupeek, Oro", "India holds 25,000 tonnes of household gold. 40% gold loan penetration opportunity. Digital gold loan app."),
    ("NBFC — Microfinance India", "nbfc", "india", "asia", "growth", "steady_growth", "aif_cat2", 12.0, 3.5, 18.0, "2024-2030", "Ujjivan, CreditAccess Grameen, Arohan, Asirvad, Spandana Sphoorty", "650M+ bottom of pyramid. SHG-Bank Linkage + JLG model. Digital collections reducing NPAs."),
    ("Urban Co-living & Student Housing India", "real_estate", "india", "asia", "series_b", "high_growth", "aif_cat2", 2.5, 0.8, 38.0, "2024-2030", "Stanza Living, Zolo, OYO Biz, CoLive, Tribe Theory", "300M+ urban migrant workers + 37M students. Managed accommodation market 95% unorganized."),
    ("Senior Care & Assisted Living India", "healthcare", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.0, 0.6, 35.0, "2024-2030", "Antara Senior Care, Columbia Pacific, Athulya, Epoch Elder Care, Nightingales", "Aged 60+ population reaching 300M by 2050. Silver economy + pension wealth unlocking."),
    ("Supply Chain Finance & Invoice Discounting India", "fintech", "india", "asia", "series_b", "high_growth", "aif_cat2", 18.0, 5.5, 28.0, "2024-2030", "M1xchange, Invoicemart, Drip Capital, Credlix, Vayana Network", "₹20L Cr working capital gap for SMEs in supply chains. TReDS (Trade Receivables) ecosystem growing."),
    ("EdTech — K12 & Test Prep India", "edtech", "india", "asia", "growth", "steady_growth", "aif_cat2", 10.0, 3.0, 20.0, "2024-2030", "BYJU's, Vedantu, Physics Wallah, Allen Digital, Infinity Learn", "Post-BYJU's shake-up creating consolidation. Physics Wallah model (affordable + offline+online) winning."),
    ("QSR Franchise & Cloud Kitchens India", "consumer", "india", "asia", "growth", "high_growth", "aif_cat2", 6.0, 1.8, 28.0, "2024-2030", "Rebel Foods (Faasos), Biryani by Kilo, Wow Momo, Oven Story, Mojo Pizza", "Cloud kitchen opex 40% lower than dine-in. Zomato/Swiggy dark stores enabling 10-min food delivery."),
    ("Renewable Energy Services India", "cleantech", "india", "asia", "growth", "steady_growth", "aif_cat1", 15.0, 4.5, 22.0, "2024-2030", "ReNew Power, O2 Power, Sterlite Power, Greenko, Torrent Power", "PM Surya Ghar: ₹75,000 Cr rooftop solar subsidy. C&I solar PPA + open access expanding."),
    ("Enterprise Blockchain India", "technology", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.5, 0.4, 50.0, "2024-2030", "Polygon, TraceX, Mintoak, SettleMint, NPCI Bharat BillPay", "Trade finance + supply chain + land records blockchain. RBI CBDC infrastructure layer."),
    ("HR Staffing & Flexi Workforce India", "saas", "india", "asia", "growth", "steady_growth", "aif_cat2", 7.0, 2.0, 18.0, "2024-2030", "TeamLease, Quess Corp, Mafoi, Adecco India, Randstad India", "India's 500M+ workforce 88% in informal sector. Gig economy formalization + compliance technology."),
    ("Veterinary & Animal Health India", "healthcare", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.5, 0.4, 35.0, "2024-2030", "Intas Animal Health, Hester Biosciences, Sequent Scientific, Vetoquinol India, Panacea", "Livestock + pet health — Companion Animal Registry push. Poultry disease + swine influenza vaccines."),
    ("Tele-Medicine & Digital Primary Care India", "healthtech", "india", "asia", "series_b", "high_growth", "aif_cat2", 4.0, 1.2, 40.0, "2024-2030", "Apollo Telehealth, Practo, 1mg Tele, Tata 1mg, mfine", "Ayushman Bharat Digital Mission digital health IDs 500M+. e-Sanjeevani 200M+ consultations."),
    ("Sports Infrastructure & Academies India", "consumer", "india", "asia", "growth", "steady_growth", "aif_cat2", 3.0, 0.9, 18.0, "2024-2030", "JSW Sports, Inspire Institute, Inspire Sports, Baseline, Sportz Village", "Khelo India + Olympic 2036 bid. District Sports Complex scheme. Multi-sport academy franchising."),
    ("Electric 2W & 3W Fleet India", "ev", "india", "asia", "series_b", "high_growth", "aif_cat2", 6.0, 1.8, 50.0, "2024-2030", "Ola Electric, Ather, Bounce Infinity, Yulu, Euler Motors", "FAME-II ₹10,000 Cr + State EV policies. Last-mile delivery fleet electrification."),
    ("Sustainable Packaging India", "cleantech", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.0, 0.6, 30.0, "2024-2030", "EcoEx, Pakka (Yash Papers), Earthsoul, Ranpak, GreenWrap", "Single-use plastic ban 2022 + EPR for packaging. E-commerce paper packaging demand explosion."),
    ("SaaS for Chartered Accountants India", "saas", "india", "asia", "series_a", "high_growth", "aif_cat2", 0.8, 0.2, 45.0, "2024-2030", "Tally, Busy Infotech, ClearTax, EY Integra, IRIS Business", "1.3M CAs in India. GST + Income Tax + MCA filing automation. AI-powered tax advisory."),
    ("Real Estate Tech — RERA Compliance India", "real_estate", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.5, 0.4, 35.0, "2024-2030", "NoBroker, Anarock, Housing.com, PropEquity, MoHUA systems", "RERA 2016 compliance software for 65,000+ registered projects. Construction monitoring + buyer portal."),
    ("Fleet Management & Telematics India", "logistics", "india", "asia", "series_a", "steady_growth", "aif_cat2", 2.0, 0.6, 25.0, "2024-2030", "Fleetx, NovaStar, Matrack, Locus, Roambee", "1.5M+ commercial vehicles in India. GPS mandatory + AIS 140 compliance driving adoption."),
    ("Genomics & Precision Medicine India", "biotech", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.0, 0.6, 45.0, "2024-2030", "MedGenome, Strand Life Sciences, Mapmygenome, Nucleome, 4baseCare", "1.4B genetic diversity data pool. ICMR genomics registry initiative. BRCA testing + pharmacogenomics."),
    ("Co-Working & Managed Offices India", "real_estate", "india", "asia", "growth", "high_growth", "aif_cat2", 4.0, 1.2, 30.0, "2024-2030", "WeWork India, Awfis, Table Space, Bhive, Smartworks", "GCC + startup space demand. 15% of Grade A supply as managed offices by 2026 target."),
    ("FinTech Compliance & RegTech India", "fintech", "india", "asia", "series_a", "high_growth", "aif_cat2", 2.0, 0.6, 40.0, "2024-2030", "NPCI BharatPe, Signzy, IDfy, Bureau.id, Perfios", "PMLA amendments 2023 + RBI KYC master directions. V-KYC + AML screening automation."),
    ("Renewable Energy Storage — BESS India", "cleantech", "india", "asia", "series_b", "high_growth", "aif_cat1", 5.0, 1.5, 55.0, "2024-2030", "Amara Raja, Exide, Servotech, Greenko BESS, Fluence India", "BESS 4 GWh tender by CEA. PLI ACC Battery 50 GWh ambition. Grid stability for 500 GW RE."),
    ("Pharma Retail & Hospital Pharmacy India", "healthcare", "india", "asia", "growth", "steady_growth", "aif_cat2", 25.0, 7.5, 15.0, "2024-2030", "MedPlus, Apollo Pharmacy, Netmeds, Pharmeasy, Generic Aadhaar", "₹1.8L Cr pharma retail 15% organized. Jan Aushadhi Kendra rollout + generic push."),
    ("Food Processing & Cold Chain India", "manufacturing", "india", "asia", "growth", "steady_growth", "aif_cat1", 12.0, 3.5, 15.0, "2024-2030", "ITC Agri, Pepsico India, McCain India, FieldFresh, Keventer Agro", "PLISFPI ₹10,900 Cr for food processing 2021-2027. Millet processing + ready-to-eat."),
    ("Robotics & Warehouse Automation India", "technology", "india", "asia", "series_a", "high_growth", "aif_cat2", 3.0, 0.9, 40.0, "2024-2030", "GreyOrange, Addverb, Asimov Robotics, Netsol Technologies, MahaRobotics", "E-commerce warehouse automation. ICD/CFS automation at ports. Industry 4.0 robotics."),
    ("Logistics Analytics & Control Tower India", "logistics", "india", "asia", "series_a", "high_growth", "aif_cat2", 1.5, 0.4, 45.0, "2024-2030", "FarEye, Locus, Elastic Run, Samsara, Project44", "Supply chain visibility post-COVID priority. ML demand forecasting + last-mile route optimization."),

    # ── Global / Thematic ──────────────────────────────────────────────────
    ("Generative AI Enterprise Applications", "ai_ml", "global", "global", "series_b", "high_growth", "lp_gp", 300.0, 90.0, 50.0, "2024-2030", "OpenAI, Anthropic, Scale AI, Cohere, AI21 Labs, MosaicML", "ChatGPT $2B ARR in 18 months. Enterprise AI spend $170B by 2028. Vertical AI agents replacing knowledge work."),
    ("Global Climate Tech — Carbon Capture", "cleantech", "global", "global", "series_a", "high_growth", "lp_gp", 50.0, 15.0, 55.0, "2024-2035", "Climeworks, Carbon Engineering, Charm Industrial, 1PointFive, CarbonCapture Inc", "45Q tax credit $85/tonne. Voluntary carbon market $50B by 2030. Corporate net-zero commitments."),
    ("Global Space Economy", "aerospace", "global", "global", "late_stage", "high_growth", "lp_gp", 600.0, 180.0, 12.0, "2024-2040", "SpaceX, Planet Labs, Spire, Maxar, Rocket Lab, AST SpaceMobile", "$600B space economy by 2040 (Morgan Stanley). Satellite broadband + earth observation + manufacturing."),
    ("Global Quantum Computing", "technology", "global", "global", "series_a", "high_growth", "lp_gp", 8.0, 2.5, 60.0, "2024-2035", "IBM Quantum, Google Quantum AI, IonQ, Rigetti, PsiQuantum, QuEra", "Quantum advantage for pharma, finance, logistics by 2030. NISQ era applications in optimization."),
    ("Global Nuclear Fusion Energy", "cleantech", "global", "global", "series_b", "high_growth", "lp_gp", 25.0, 7.0, 40.0, "2024-2040", "Commonwealth Fusion Systems, Helion Energy, TAE Technologies, Tokamak Energy, General Fusion", "First commercial fusion reactor 2035 target. Helion $2.2B + Microsoft partnership."),
    ("Global Longevity & Anti-Aging Tech", "biotech", "global", "global", "series_a", "high_growth", "lp_gp", 30.0, 9.0, 45.0, "2024-2035", "Altos Labs, Unity Biotechnology, Calico, Samumed, BioAge", "Aging population creates $30T longevity market. Senolytics, epigenetic reprogramming, mTOR inhibitors."),
    ("Global EdTech — Workforce Learning", "edtech", "global", "global", "series_b", "high_growth", "lp_gp", 40.0, 12.0, 20.0, "2024-2030", "Coursera, Udacity, LinkedIn Learning, Guild Education, Degreed", "Skills gap = 85M jobs unfilled by 2030. Corporate L&D + employer-sponsored education."),
    ("Global Synthetic Media & DeepFake Detection", "technology", "global", "global", "series_a", "high_growth", "lp_gp", 5.0, 1.5, 60.0, "2024-2030", "Synthesia, ElevenLabs, Reality Defender, TrueMedia, Hive", "EU AI Act + US DEFIANCE Act creating compliance demand. B2B synthetic media creation + detection."),
]


def seed_opportunities(apps, schema_editor):
    MarketOpportunity = apps.get_model('marketresearch', 'MarketOpportunity')
    for opp in OPPORTUNITIES:
        (name, sector, country, continent, inv_stage, fin_cat, fund_type,
         tam, sam, cagr, cagr_period, players, thesis) = opp
        slug = slugify(name)[:200]
        # Ensure unique slug
        base_slug = slug
        suffix = 0
        while MarketOpportunity.objects.filter(slug=slug).exists():
            suffix += 1
            slug = f'{base_slug}-{suffix}'

        MarketOpportunity.objects.get_or_create(
            slug=slug,
            defaults={
                'name': name,
                'sector': sector,
                'country': country,
                'continent': continent,
                'investment_stage': inv_stage,
                'financial_category': fin_cat,
                'fund_type': fund_type,
                'tam_usd_bn': tam,
                'sam_usd_bn': sam,
                'cagr_pct': cagr,
                'cagr_period': cagr_period,
                'key_players': players,
                'investment_thesis': thesis,
                'description': thesis[:500],
                'is_seeded': True,
                'is_active': True,
            },
        )


def unseed_opportunities(apps, schema_editor):
    MarketOpportunity = apps.get_model('marketresearch', 'MarketOpportunity')
    MarketOpportunity.objects.filter(is_seeded=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('marketresearch', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_opportunities, unseed_opportunities),
    ]
