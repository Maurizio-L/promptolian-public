(function () {
  'use strict';

  // ── Compression engine ────────────────────────────────────────────────────
  // ── Standard rules (applied always) ────────────────────────────────────────
  const PS_RULES = [
    // Roles & context
    [/you are an? expert (in |on |at )?/gi, '§EXP '],
    [/you are an? /gi, '§ROLE '],
    [/act as (a |an )?/gi, '§ACT '],
    [/pretend (you are|to be) (a |an )?/gi, '§ROLE '],
    [/here is (the |some )?context[:\s]*/gi, '§CTX '],
    [/given (this |the )?context[:\s]*/gi, '§CTX '],
    // Filler openers
    [/I would like you to /gi, ''],
    [/I need you to /gi, ''],
    [/can you (please )?/gi, ''],
    [/could you (please )?/gi, ''],
    [/please /gi, ''],
    // Output format
    [/return only (the )?code[^.]*\.?/gi, '→code'],
    [/return as (a )?bullet[- ]?list/gi, '→list'],
    [/in bullet points?/gi, '→list'],
    [/return as (a )?numbered list/gi, '→nlist'],
    [/return as (a )?table/gi, '→table'],
    [/return as json/gi, '→json'],
    [/in json format/gi, '→json'],
    [/return as yaml/gi, '→yaml'],
    [/return as (csv|markdown)/gi, (_, f) => '→' + f.toLowerCase()],
    [/step[- ]by[- ]step/gi, '→step'],
    [/pros and cons/gi, '→pros/cons'],
    [/be (very )?concise\.?/gi, '→short'],
    [/keep it (very )?short\.?/gi, '→short'],
    [/\bbriefly\b/gi, '→short'],
    [/in (simple|plain|easy) (terms|language|english)/gi, '→ELI5'],
    [/for (a )?beginner(s)?/gi, '→ELI5'],
    [/in (great |comprehensive )?detail/gi, '→long'],
    [/\bcomprehensive\b/gi, '→long'],
    [/no explanation/gi, '⊖explain'],
    [/without (any )?explanation/gi, '⊖explain'],
    // Task verbs (long patterns first)
    [/fix (the |any |a )?bug(s)?( in| on)?/gi, 'BUG'],
    [/write (a |an )?unit tests? for/gi, 'TEST'],
    [/write tests? for/gi, 'TEST'],
    [/write (a |an )?function (to |that )?/gi, 'FN '],
    [/create (a |an )?function (to |that )?/gi, 'FN '],
    [/write (a |an )?class\b/gi, 'CLS'],
    [/summarize (this |the |a )?/gi, '∑ '],
    [/summary of (the |this |a )?/gi, '∑ '],
    [/explain how\b/gi, '? how'],
    [/difference between\b/gi, '§DIFF'],
    [/compare\b/gi, '§DIFF'],
    [/\bdebug\b/gi, 'BUG'],
    [/\boptimize\b/gi, 'OPT'],
    [/\brefactor\b/gi, '∆'],
    [/\brewrite\b/gi, '∆'],
    [/\bimprove\b/gi, '∆'],
    [/\bupdate\b/gi, '∆'],
    [/\bedit\b/gi, '∆'],
    [/\banalyze\b/gi, 'ANLZ'],
    [/\banalyse\b/gi, 'ANLZ'],
    [/\bevaluate\b/gi, 'EVAL'],
    [/\bassess\b/gi, 'EVAL'],
    [/\bimplement\b/gi, 'impl'],
    [/\bgenerate\b/gi, 'GEN'],
    [/\bcreate\b/gi, 'mk'],
    [/\bbuild\b/gi, 'bld'],
    [/\btranslate\b/gi, '⇄'],
    [/\bconvert\b/gi, '⇄'],
    [/\bdescribe\b/gi, '?'],
    [/\bexplain\b/gi, '?'],
    [/\bhow does\b/gi, '?'],
    [/\bwhat (is|are)\b/gi, '?'],
    [/\bwhy (is|are|does|do)\b/gi, '?why'],
    [/\breview (my |this |the )?/gi, '« '],
    [/\brevise (my |this |the )?/gi, '« '],
    [/\bcontinue\b/gi, '»'],
    [/\bfind\b/gi, '⊂'],
    [/\bsearch (for )?\b/gi, '⊂'],
    [/\bextract\b/gi, '⊂'],
    [/\bprovide (a |an )?/gi, ''],
    [/\bgive me (a |an )?/gi, ''],
    [/\bhelp me (with )?(a |an )?/gi, ''],
    [/\bdeploy\b/gi, 'dpl'],
    [/\bconfigure\b/gi, 'cfg'],
    [/\bset up\b/gi, 'cfg'],
    [/\binstall\b/gi, 'inst'],
    [/\bdocument\b/gi, 'docs'],
    // Constraints & emphasis
    [/make sure (to |that )?/gi, '!! '],
    [/ensure (that )?/gi, '!! '],
    [/\bdo not\b/gi, '§NOT'],
    [/\bdon't\b/gi, '§NOT'],
    [/\bavoid\b/gi, '§NOT'],
    [/\bnever\b/gi, '§NOT'],
    [/\b(very )?important[:\s]+/gi, '!! '],
    [/\bcritical[:\s]+/gi, '!! '],
    [/\bmust\b/gi, '!!'],
    // Logic & connectors
    [/\balso\b/gi, '§ALSO'],
    [/\badditionally\b/gi, '§ALSO'],
    [/\balternative(ly)?\b/gi, '§ALT'],
    [/\binstead\b/gi, '§ALT'],
    [/\bbest practice(s)?\b/gi, '§BEST'],
    [/\brecommended\b/gi, '§BEST'],
    [/\bfor example\b/gi, '§EX'],
    [/\bfor instance\b/gi, '§EX'],
    [/\bcontext\b/gi, '§CTX'],
    [/\boutput\b/gi, '→'],
    [/\bgiven\b/gi, '←'],
    [/\binput\b/gi, '←'],
    // Quantifiers
    [/\ball\b/gi, '∀'],
    [/\beach\b/gi, '∀'],
    [/\bevery\b/gi, '∀'],
    [/\bat least\b/gi, 'min'],
    [/\bat most\b/gi, 'max'],
    [/\bmore than\b/gi, '>'],
    [/\bless than\b/gi, '<'],
    [/\bapproximately\b/gi, '~'],
    // ── Tech vocabulary ───────────────────────────────────────────────────────
    [/\bmachine learning\b/gi, 'ML'],
    [/\bartificial intelligence\b/gi, 'AI'],
    [/\blarge language model(s)?\b/gi, 'LLM'],
    [/\bnatural language processing\b/gi, 'NLP'],
    [/\bdeep learning\b/gi, 'DL'],
    [/\bneural network(s)?\b/gi, 'NN'],
    [/\bunit test(s)?\b/gi, 'TEST'],
    [/\bpull request(s)?\b/gi, 'PR'],
    [/\buser interface\b/gi, 'UI'],
    [/\buser experience\b/gi, 'UX'],
    [/\bcommand[- ]line( interface)?\b/gi, 'CLI'],
    [/\brest api\b/gi, 'REST'],
    [/\bci\/cd\b/gi, 'CI/CD'],
    [/\bcontinuous integration\b/gi, 'CI'],
    [/\bcontinuous deployment\b/gi, 'CD'],
    [/\bversion control\b/gi, 'VCS'],
    [/\bobject[- ]oriented\b/gi, 'OOP'],
    [/\bdesign pattern(s)?\b/gi, 'DP'],
    [/\bdatabase\b/gi, 'DB'],
    [/\brepository\b/gi, 'repo'],
    [/\bapplication\b/gi, 'app'],
    [/\barchitecture\b/gi, 'arch'],
    [/\binfrastructure\b/gi, 'infra'],
    [/\bdeployment\b/gi, 'dpl'],
    [/\benvironment\b/gi, 'env'],
    [/\bconfiguration\b/gi, 'cfg'],
    [/\bdependenc(y|ies)\b/gi, 'dep'],
    [/\bauthentication\b/gi, 'auth'],
    [/\bauthorization\b/gi, 'authz'],
    [/\basynchronous\b/gi, 'async'],
    [/\bsynchronous\b/gi, 'sync'],
    [/\bendpoint(s)?\b/gi, 'EP'],
    [/\bmiddleware\b/gi, 'MW'],
    [/\bframework\b/gi, 'fw'],
    [/\blibrary\b/gi, 'lib'],
    [/\bpackage\b/gi, 'pkg'],
    [/\bperformance\b/gi, 'perf'],
    [/\bscalabilit(y|ies)\b/gi, 'scale'],
    [/\blatency\b/gi, 'lat'],
    [/\bthroughput\b/gi, 'tput'],
    [/\bsecurity\b/gi, 'SEC'],
    [/\bvulnerabilit(y|ies)\b/gi, 'VULN'],
    [/\bencryption\b/gi, 'ENC'],
    [/\bvalidation\b/gi, 'val'],
    [/\bpipeline\b/gi, 'pipe'],
    [/\bmicroservice(s)?\b/gi, 'µsvc'],
    [/\bcontainer(s)?\b/gi, 'ctr'],
    [/\bfunction\b/gi, 'FN'],
    [/\bmethod\b/gi, 'FN'],
    [/\bclass\b/gi, 'CLS'],
    // Programming languages
    [/\bpython\b/gi, 'py'],
    [/\bjavascript\b/gi, 'js'],
    [/\btypescript\b/gi, 'ts'],
    [/\bgolang\b/gi, 'go'],
    [/\bkotlin\b/gi, 'kt'],
    [/\bc\+\+\b/gi, 'cpp'],
    [/\bruby\b/gi, 'rb'],
    [/\bbash\b/gi, 'sh'],
    [/\bshell script\b/gi, 'sh'],
    // ── Finance vocabulary ────────────────────────────────────────────────────
    [/\bcompound annual growth rate\b/gi, 'CAGR'],
    [/\bcustomer acquisition cost\b/gi, 'CAC'],
    [/\bcustomer lifetime value\b/gi, 'LTV'],
    [/\bmonthly recurring revenue\b/gi, 'MRR'],
    [/\bannual recurring revenue\b/gi, 'ARR'],
    [/\bgross merchandise value\b/gi, 'GMV'],
    [/\baverage revenue per user\b/gi, 'ARPU'],
    [/\breturn on investment\b/gi, 'ROI'],
    [/\bnet present value\b/gi, 'NPV'],
    [/\bearnings per share\b/gi, 'EPS'],
    [/\bkey performance indicator(s)?\b/gi, 'KPI'],
    [/\bprofit and loss\b/gi, 'P&L'],
    [/\byear[- ]over[- ]year\b/gi, 'YoY'],
    [/\bquarter[- ]over[- ]quarter\b/gi, 'QoQ'],
    [/\bmonth[- ]over[- ]month\b/gi, 'MoM'],
    [/\btotal addressable market\b/gi, 'TAM'],
    [/\bnet revenue retention\b/gi, 'NRR'],
    [/\bnet promoter score\b/gi, 'NPS'],
    [/\bbasis points?\b/gi, 'bps'],
    [/\bhead ?count\b/gi, 'HC'],
    [/\bcash flow\b/gi, 'CF'],
    [/\baccounts receivable\b/gi, 'AR'],
    [/\baccounts payable\b/gi, 'AP'],
    [/\bbalance sheet\b/gi, 'BS'],
    [/\boperating expenditure\b/gi, 'OpEx'],
    [/\bcapital expenditure\b/gi, 'CapEx'],
    [/\bprofit margin\b/gi, 'margin'],
    [/\bforecast\b/gi, 'fcst'],
    [/\bquarterly\b/gi, 'qtrly'],
    [/\bannual(ly)?\b/gi, 'ann'],
    // ── Law vocabulary ────────────────────────────────────────────────────────
    [/\bnon[- ]disclosure agreement\b/gi, 'NDA'],
    [/\bterms of service\b/gi, 'ToS'],
    [/\bterms and conditions\b/gi, 'T&C'],
    [/\bintellectual property\b/gi, 'IP'],
    [/\blimited liability company\b/gi, 'LLC'],
    [/\bgeneral data protection regulation\b/gi, 'GDPR'],
    [/\bservice level agreement\b/gi, 'SLA'],
    [/\bcalifornia consumer privacy act\b/gi, 'CCPA'],
    [/\bindemnif(y|ication)\b/gi, 'indem'],
    [/\bliabilit(y|ies)\b/gi, 'liab'],
    [/\bjurisdiction\b/gi, 'jxn'],
    [/\bregulation(s)?\b/gi, 'reg'],
    [/\bcompliance\b/gi, 'compl'],
    [/\bclause\b/gi, 'cl'],
    [/\bamendment\b/gi, 'amdt'],
    [/\bdamages\b/gi, 'dmg'],
    [/\bbreach\b/gi, 'brch'],
    [/\bprivacy policy\b/gi, 'PrivPol'],
    // ── Data science vocabulary ───────────────────────────────────────────────
    [/\bconvolutional neural network(s)?\b/gi, 'CNN'],
    [/\brecurrent neural network(s)?\b/gi, 'RNN'],
    [/\bprincipal component analysis\b/gi, 'PCA'],
    [/\bexploratory data analysis\b/gi, 'EDA'],
    [/\bk[- ]nearest neighbor(s)?\b/gi, 'KNN'],
    [/\bsupport vector machine(s)?\b/gi, 'SVM'],
    [/\bgradient (boosting|descent)\b/gi, (_, t) => t === 'descent' ? 'GD' : 'GB'],
    [/\bcross[- ]validation\b/gi, 'CV'],
    [/\bhyperparameter(s)?\b/gi, 'HP'],
    [/\blearning rate\b/gi, 'lr'],
    [/\barea under the curve\b/gi, 'AUC'],
    [/\broot mean square error\b/gi, 'RMSE'],
    [/\bmean absolute error\b/gi, 'MAE'],
    [/\bF1[- ]score\b/gi, 'F1'],
    [/\btime series\b/gi, 'TS'],
    [/\bdimensionality reduction\b/gi, 'DR'],
    [/\bfeature engineering\b/gi, 'FE'],
    [/\bfeature selection\b/gi, 'FS'],
    [/\bdata frame\b/gi, 'df'],
    [/\bdata warehouse\b/gi, 'DWH'],
    [/\bbusiness intelligence\b/gi, 'BI'],
    [/\bextract[,]? transform[,]? load\b/gi, 'ETL'],
    [/\boverfitting\b/gi, 'overfit'],
    [/\bunderfitting\b/gi, 'underfit'],
    [/\bone[- ]hot encoding\b/gi, 'OHE'],
    [/\bmissing values?\b/gi, 'NaN'],
    [/\btraining (data|set)\b/gi, 'train_data'],
    [/\bvalidation (data|set)\b/gi, 'val_data'],
    [/\btest (data|set)\b/gi, 'test_data'],
    // ── Social / marketing vocabulary ─────────────────────────────────────────
    [/\bcall to action\b/gi, 'CTA'],
    [/\bclick[- ]through rate\b/gi, 'CTR'],
    [/\bconversion rate\b/gi, 'CVR'],
    [/\bcost per (click|acquisition)\b/gi, (_, t) => t === 'click' ? 'CPC' : 'CPA'],
    [/\bsearch engine optimization\b/gi, 'SEO'],
    [/\bsearch engine marketing\b/gi, 'SEM'],
    [/\bpay[- ]per[- ]click\b/gi, 'PPC'],
    [/\buser[- ]generated content\b/gi, 'UGC'],
    [/\bengagement rate\b/gi, 'ER'],
    [/\bkey opinion leader(s)?\b/gi, 'KOL'],
    [/\bvalue proposition\b/gi, 'VP'],
    [/\btarget audience\b/gi, 'TA'],
    [/\bcontent calendar\b/gi, 'CC'],
    [/\bbrand awareness\b/gi, 'BA'],
    [/\bsocial media\b/gi, 'SM'],
    [/\binfluencer(s)?\b/gi, 'infl'],
    [/\bsubscriber(s)?\b/gi, 'sub'],
    [/\bfollower(s)?\b/gi, 'flw'],
    [/\bcommunity manager\b/gi, 'CM'],
    [/ +/g, ' '],
  ];

  // ── Telegraphic rules (opt-in — applied on top of standard) ─────────────────
  // Removes grammatical glue: articles, copulas, common filler.
  // Only activated when caller passes telegraphic=true.
  const TELEGRAPHIC_RULES = [
    // Copula + article
    [/(is|are|was|were) (a|an|the) /gi, ''],
    [/(has|have|had) (a|an|the) /gi, ''],
    // Standalone articles at word boundary
    [/\bthe\b /gi, ''],
    [/\ba\b /gi, ''],
    [/\ban\b /gi, ''],
    // Common filler adverbs
    [/\bcurrently\b/gi, ''],
    [/\bactually\b/gi, ''],
    [/\bessentially\b/gi, ''],
    [/\bbasically\b/gi, ''],
    [/\bsimply\b/gi, ''],
    [/\bjust\b/gi, ''],
    // "there is/are"
    [/\bthere (is|are|was|were) /gi, ''],
    [/\bit (is|was) /gi, ''],
    // Reporting connectives
    [/\b(reported|stated|noted) that\b/gi, ':'],
    [/\bwhich (is|was|are|were) /gi, ''],
    [/\bthat (is|was) /gi, ''],
    // Prepositions in instruction context
    [/\bin order to\b/gi, 'to'],
    [/\bso that\b/gi, '→'],
    [/ +/g, ' '],
  ];

  // ── Domain packs (Pro — opt-in vocabulary sets) ───────────────────────────
  const DOMAIN_PACKS = {
    medical: [
      [/\bblood pressure\b/gi, 'BP'],
      [/\bheart rate\b/gi, 'HR'],
      [/\belectrocardiogram\b/gi, 'ECG'],
      [/\bmagnetic resonance imaging\b/gi, 'MRI'],
      [/\bcomputed tomography\b/gi, 'CT'],
      [/\bemergency (department|room)\b/gi, (_, r) => r === 'room' ? 'ER' : 'ED'],
      [/\bintensive care unit\b/gi, 'ICU'],
      [/\bmyocardial infarction\b/gi, 'MI'],
      [/\bdiabetes mellitus\b/gi, 'DM'],
      [/\bhypertension\b/gi, 'HTN'],
      [/\bchronic obstructive pulmonary disease\b/gi, 'COPD'],
      [/\bcongestive heart failure\b/gi, 'CHF'],
      [/\batrial fibrillation\b/gi, 'AFib'],
      [/\bdeep vein thrombosis\b/gi, 'DVT'],
      [/\bpulmonary embolism\b/gi, 'PE'],
      [/\burinary tract infection\b/gi, 'UTI'],
      [/\bnon[- ]steroidal anti[- ]inflammatory drug(s)?\b/gi, 'NSAID'],
      [/\bphysical therapy\b/gi, 'PT'],
      [/\boccupational therapy\b/gi, 'OT'],
      [/\bactivities of daily living\b/gi, 'ADL'],
      [/\brange of motion\b/gi, 'ROM'],
      [/\bdiagnosis\b/gi, 'Dx'],
      [/\btreatment\b/gi, 'Tx'],
      [/\bprescription\b/gi, 'Rx'],
      [/\bchief complaint\b/gi, 'CC'],
      [/\bpast medical history\b/gi, 'PMH'],
      [/\bhistory of present illness\b/gi, 'HPI'],
      [/\bcomplete blood count\b/gi, 'CBC'],
      [/\bcomprehensive metabolic panel\b/gi, 'CMP'],
      [/\bbasic metabolic panel\b/gi, 'BMP'],
      [/\bwhite blood cells?\b/gi, 'WBC'],
      [/\bred blood cells?\b/gi, 'RBC'],
      [/\bglomerular filtration rate\b/gi, 'GFR'],
      [/\bblood urea nitrogen\b/gi, 'BUN'],
      [/\btype 2 diabetes\b/gi, 'T2DM'],
      [/\btype 1 diabetes\b/gi, 'T1DM'],
      [/\bcardiovascular disease\b/gi, 'CVD'],
      [/\bbody mass index\b/gi, 'BMI'],
      [/\bcentral nervous system\b/gi, 'CNS'],
    ],
    academic: [
      [/\brandomized controlled trial(s)?\b/gi, 'RCT'],
      [/\bsystematic review\b/gi, 'sys rev'],
      [/\bliterature review\b/gi, 'lit rev'],
      [/\bconfidence interval\b/gi, 'CI'],
      [/\bstandard deviation\b/gi, 'SD'],
      [/\bstandard error\b/gi, 'SE'],
      [/\bstatistical(ly)? significant\b/gi, 'sig.'],
      [/\bindependent variable\b/gi, 'IV'],
      [/\bdependent variable\b/gi, 'DV'],
      [/\bnull hypothesis\b/gi, 'H₀'],
      [/\balternative hypothesis\b/gi, 'H₁'],
      [/\bqualitative\b/gi, 'qual'],
      [/\bquantitative\b/gi, 'quant'],
      [/\bresearch question\b/gi, 'RQ'],
      [/\btheoretical framework\b/gi, 'theory fw'],
      [/\bthematic analysis\b/gi, 'TA'],
      [/\bsemi[- ]structured interview\b/gi, 'SSI'],
      [/\bfocus group(s)?\b/gi, 'FG'],
      [/\binformed consent\b/gi, 'IC'],
      [/\bpeer[- ]reviewed\b/gi, 'peer-rev'],
      [/\bopen[- ]access\b/gi, 'OA'],
      [/\bsample size\b/gi, 'n'],
      [/\beffect size\b/gi, 'ES'],
      [/\banalysis of variance\b/gi, 'ANOVA'],
      [/\bmultivariate analysis\b/gi, 'MVA'],
      [/\bregression analysis\b/gi, 'reg'],
      [/\bdouble[- ]blind\b/gi, 'DB'],
      [/\bplacebo[- ]controlled\b/gi, 'PC'],
      [/\bethics (committee|board)\b/gi, 'IRB'],
      [/\bcase study\b/gi, 'CS'],
      [/\bgrounded theory\b/gi, 'GT'],
      [/\bethnograph(y|ic)\b/gi, 'ethnog'],
      [/\binter[- ]rater reliability\b/gi, 'IRR'],
      [/\bcoefficient of variation\b/gi, 'CV'],
      [/\bdegrees of freedom\b/gi, 'df'],
    ],
    legal_pro: [
      [/\bplaintiff\b/gi, 'pltf'],
      [/\bdefendant\b/gi, 'def'],
      [/\battorney\b/gi, 'atty'],
      [/\bdeposition\b/gi, 'depo'],
      [/\bmotion for summary judgment\b/gi, 'MSJ'],
      [/\bpreliminary injunction\b/gi, 'PI'],
      [/\btemporary restraining order\b/gi, 'TRO'],
      [/\bstatute of limitations\b/gi, 'SOL'],
      [/\bdue diligence\b/gi, 'DD'],
      [/\bforce majeure\b/gi, 'FM'],
      [/\bliquidated damages\b/gi, 'LD'],
      [/\bnon[- ]compete agreement\b/gi, 'NCA'],
      [/\barbitration\b/gi, 'arb'],
      [/\bmediation\b/gi, 'med'],
      [/\baffidavit\b/gi, 'aff'],
      [/\bsubpoena\b/gi, 'subp'],
      [/\bdiscovery\b/gi, 'discov'],
      [/\binterrogator(y|ies)\b/gi, 'interrog'],
      [/\bclass action\b/gi, 'CA'],
      [/\bsettlement agreement\b/gi, 'SA'],
      [/\bindemnification\b/gi, 'indem'],
      [/\brepresentations and warranties\b/gi, 'R&W'],
      [/\bclosing conditions\b/gi, 'CC'],
      [/\bmaterial adverse change\b/gi, 'MAC'],
      [/\bearn[- ]out\b/gi, 'earnout'],
    ],
  };

  // ── Pro settings state ────────────────────────────────────────────────────
  let activePacks = [];
  let customRules = [];
  let autoCompress = false;
  let _quotaCache = { ok: true, pro: false, count: 0 };

  function loadProSettings() {
    chrome.storage.local.get(
      ['promptly_license', 'promptly_packs', 'promptly_custom_rules', 'promptly_auto', 'promptly_daily'],
      res => {
        if (!res.promptly_license) return;
        activePacks = res.promptly_packs || [];
        customRules = (res.promptly_custom_rules || []).map(r => {
          try { return [new RegExp('\\b' + r.pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'gi'), r.replacement]; }
          catch (_) { return null; }
        }).filter(Boolean);
        autoCompress = res.promptly_auto === true;
        // Refresh quota cache
        const today = getTodayKey();
        const daily = (res.promptly_daily && res.promptly_daily.date === today) ? res.promptly_daily : { date: today, count: 0 };
        _quotaCache = { ok: res.promptly_license ? true : daily.count < FREE_LIMIT, pro: !!res.promptly_license, count: daily.count };
      }
    );
  }

  function trackCompression(et, pt, site) {
    const entry = { ts: Date.now(), site, et, pt, pct: et > 0 ? Math.round((et - pt) / et * 100) : 0 };
    chrome.storage.local.get(['promptly_history'], res => {
      const hist = (res.promptly_history || []);
      hist.unshift(entry);
      chrome.storage.local.set({ promptly_history: hist.slice(0, 500) });
    });
  }

  function psEncode(text, telegraphic = false, packs = [], custom = []) {
    let out = text.trim();
    for (const [pat, rep] of PS_RULES) out = out.replace(pat, rep);
    for (const pack of packs) {
      for (const rule of (DOMAIN_PACKS[pack] || [])) out = out.replace(rule[0], rule[1]);
    }
    for (const [pat, rep] of custom) out = out.replace(pat, rep);
    if (telegraphic) {
      for (const [pat, rep] of TELEGRAPHIC_RULES) out = out.replace(pat, rep);
    }
    return out.replace(/\. /g, '.\n').replace(/  +/g, ' ').trim();
  }

  function countTokens(text) {
    return Math.ceil(text.trim().split(/[\s,.:;!?()\[\]{}"']+/).filter(Boolean).length * 1.3);
  }

  // ── Input helpers ─────────────────────────────────────────────────────────
  function setContentEditable(el, t) {
    el.focus();
    // Select all and replace — fires DOM events React/Lexical/Quill observe
    document.execCommand('selectAll', false, null);
    if (!document.execCommand('insertText', false, t)) {
      // execCommand blocked: fallback via selection + InputEvent
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      el.innerText = t;
      el.dispatchEvent(new InputEvent('input', { bubbles: true, data: t, inputType: 'insertText' }));
    }
  }

  function setTextarea(el, t) {
    el.focus();
    try {
      const nativeSet = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      nativeSet.call(el, t);
    } catch (_) {
      el.value = t;
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // ── Site selectors ────────────────────────────────────────────────────────
  const SITES = {
    'claude.ai': {
      // Try increasingly general selectors — Claude's Lexical editor uses enterkeyhint
      input: () =>
        document.querySelector('div[enterkeyhint="enter"][contenteditable="true"]') ||
        document.querySelector('div[data-placeholder][contenteditable="true"]') ||
        document.querySelector('fieldset div[contenteditable="true"]') ||
        document.querySelector('.ProseMirror[contenteditable="true"]') ||
        document.querySelector('div[contenteditable="true"]'),
      getText: (el) => el.innerText,
      setText: setContentEditable,
    },
    'chatgpt.com': {
      // ChatGPT's textarea is now a contenteditable div; keep #prompt-textarea as primary
      input: () =>
        document.querySelector('#prompt-textarea') ||
        document.querySelector('div[contenteditable="true"][data-id]') ||
        document.querySelector('main div[contenteditable="true"]'),
      getText: (el) => el.tagName === 'TEXTAREA' ? el.value : el.innerText,
      setText: (el, t) => el.tagName === 'TEXTAREA' ? setTextarea(el, t) : setContentEditable(el, t),
    },
    'chat.openai.com': {
      input: () =>
        document.querySelector('#prompt-textarea') ||
        document.querySelector('div[contenteditable="true"]'),
      getText: (el) => el.tagName === 'TEXTAREA' ? el.value : el.innerText,
      setText: (el, t) => el.tagName === 'TEXTAREA' ? setTextarea(el, t) : setContentEditable(el, t),
    },
    'gemini.google.com': {
      input: () =>
        document.querySelector('.ql-editor[contenteditable="true"]') ||
        document.querySelector('div[contenteditable="true"]'),
      getText: (el) => el.innerText,
      setText: setContentEditable,
    },
    'copilot.microsoft.com': {
      input: () =>
        document.querySelector('textarea[placeholder]') ||
        document.querySelector('div[contenteditable="true"]'),
      getText: (el) => el.tagName === 'TEXTAREA' ? el.value : el.innerText,
      setText: (el, t) => el.tagName === 'TEXTAREA' ? setTextarea(el, t) : setContentEditable(el, t),
    },
  };

  function getSite() {
    const host = location.hostname;
    for (const [k, v] of Object.entries(SITES)) {
      if (host.includes(k)) return v;
    }
    return null;
  }

  function getInput() {
    const site = getSite();
    return site ? site.input() : null;
  }

  // ── Quota helpers (free: 10/day) ──────────────────────────────────────────
  const FREE_LIMIT = 10;

  function getTodayKey() {
    return new Date().toISOString().slice(0, 10); // 'YYYY-MM-DD'
  }

  function checkQuota() {
    return new Promise(resolve => {
      chrome.storage.local.get(['promptly_license', 'promptly_daily'], res => {
        if (res.promptly_license) return resolve({ ok: true, pro: true });
        const today = getTodayKey();
        const daily = (res.promptly_daily && res.promptly_daily.date === today)
          ? res.promptly_daily : { date: today, count: 0 };
        resolve({ ok: daily.count < FREE_LIMIT, count: daily.count, pro: false });
      });
    });
  }

  function incrementQuota() {
    return new Promise(resolve => {
      chrome.storage.local.get(['promptly_daily'], res => {
        const today = getTodayKey();
        const prev = (res.promptly_daily && res.promptly_daily.date === today)
          ? res.promptly_daily : { date: today, count: 0 };
        const next = { date: today, count: prev.count + 1 };
        chrome.storage.local.set({ promptly_daily: next });
        resolve(next.count);
      });
    });
  }

  // ── State ─────────────────────────────────────────────────────────────────
  let isCompressed = false;
  let originalText = '';
  let sessionSaved = 0;
  let teleMode = false;

  // ── Toolbar ───────────────────────────────────────────────────────────────
  function injectToolbar() {
    if (document.getElementById('promptly-bar')) return;
    const site = getSite();
    if (!site) return;
    // Don't inject until the input exists
    if (!getInput()) return;

    const bar = document.createElement('div');
    bar.id = 'promptly-bar';
    bar.innerHTML = `
      <span class="p-logo">P→</span>
      <span class="p-stats" id="p-stats">Promptly ready</span>
      <div class="p-actions">
        <button class="p-btn p-compress" id="p-compress">⚡ Compress</button>
        <button class="p-btn p-tele" id="p-tele" title="Telegraphic mode strips articles &amp; copulas for max compression">Tele: OFF</button>
        <button class="p-btn p-undo" id="p-undo" style="display:none">↩ Restore</button>
        <button class="p-btn p-toggle" id="p-toggle">ON</button>
      </div>
    `;
    document.body.appendChild(bar);

    chrome.storage.local.get(['promptly_enabled', 'promptly_tele'], (res) => {
      updateToggle(res.promptly_enabled !== false);
      teleMode = res.promptly_tele === true;
      updateTele(teleMode);
    });

    document.getElementById('p-compress').addEventListener('click', async () => {
      const site = getSite();
      const input = getInput();
      if (!input) return;
      const text = site.getText(input);
      if (!text.trim()) return;
      if (isCompressed) return;

      const quota = await checkQuota();
      if (!quota.ok) {
        showUpgradeNotice(quota.count);
        return;
      }

      originalText = text;
      const compressed = psEncode(text, teleMode, activePacks, customRules);
      site.setText(input, compressed);
      const et = countTokens(text), pt = countTokens(compressed);
      const saved = Math.max(0, et - pt);
      const pct = et > 0 ? Math.round(saved / et * 100) : 0;
      sessionSaved += saved;

      const usedToday = await incrementQuota();
      _quotaCache.count = usedToday;
      if (!quota.pro && usedToday >= FREE_LIMIT) _quotaCache.ok = false;
      const remaining = quota.pro ? '∞' : `${FREE_LIMIT - usedToday} left today`;

      trackCompression(et, pt, location.hostname);

      document.getElementById('p-stats').innerHTML =
        `<span style="color:#4ade80;font-weight:600">${pct}% saved</span> · ${et}→${pt} tokens · ${remaining}`;
      isCompressed = true;
      const btn = document.getElementById('p-compress');
      btn.textContent = '✓ Compressed';
      btn.style.opacity = '0.6';
      document.getElementById('p-undo').style.display = 'inline-flex';
      chrome.runtime.sendMessage({ type: 'STATS', saved: pct });
    });

    document.getElementById('p-tele').addEventListener('click', () => {
      teleMode = !teleMode;
      chrome.storage.local.set({ promptly_tele: teleMode });
      updateTele(teleMode);
    });

    document.getElementById('p-undo').addEventListener('click', () => {
      const site = getSite();
      const input = getInput();
      if (!input || !originalText) return;
      site.setText(input, originalText);
      isCompressed = false;
      originalText = '';
      document.getElementById('p-compress').textContent = '⚡ Compress';
      document.getElementById('p-compress').style.opacity = '1';
      document.getElementById('p-undo').style.display = 'none';
      document.getElementById('p-stats').textContent = 'Promptly ready';
    });

    document.getElementById('p-toggle').addEventListener('click', () => {
      chrome.storage.local.get(['promptly_enabled'], (res) => {
        const next = !(res.promptly_enabled !== false);
        chrome.storage.local.set({ promptly_enabled: next });
        updateToggle(next);
      });
    });

    document.addEventListener('input', (e) => {
      const input = getInput();
      if (e.target === input && isCompressed) {
        isCompressed = false;
        document.getElementById('p-compress').textContent = '⚡ Compress';
        document.getElementById('p-compress').style.opacity = '1';
        document.getElementById('p-undo').style.display = 'none';
      }
    });
  }

  function updateToggle(enabled) {
    const btn = document.getElementById('p-toggle');
    if (!btn) return;
    btn.textContent = enabled ? 'ON' : 'OFF';
    btn.style.background = enabled ? '#22c55e22' : 'transparent';
    btn.style.color = enabled ? '#16a34a' : 'inherit';
    const cb = document.getElementById('p-compress');
    if (cb) cb.style.display = enabled ? 'inline-flex' : 'none';
  }

  function showUpgradeNotice(count) {
    let notice = document.getElementById('p-upgrade');
    if (notice) { notice.style.display = 'flex'; return; }
    notice = document.createElement('div');
    notice.id = 'p-upgrade';
    notice.innerHTML = `
      <span style="font-size:11px;color:#f87171">
        Daily limit reached (${count}/${FREE_LIMIT}) ·
      </span>
      <a href="https://promptly.so/#pricing" target="_blank"
         style="font-size:11px;color:#c8f135;text-decoration:none;font-weight:600;margin-left:4px">
        Upgrade to Pro →
      </a>
      <button onclick="document.getElementById('p-upgrade').style.display='none'"
              style="background:none;border:none;color:#555;cursor:pointer;font-size:13px;padding:0 0 0 8px;line-height:1">✕</button>
    `;
    Object.assign(notice.style, {
      display: 'flex', alignItems: 'center', gap: '4px',
      position: 'fixed', bottom: '62px', right: '18px',
      background: '#1a0a0a', border: '1px solid #7f1d1d',
      borderRadius: '10px', padding: '8px 12px', zIndex: '999999',
      fontFamily: '-apple-system, BlinkMacSystemFont, sans-serif',
      boxShadow: '0 4px 16px rgba(0,0,0,0.5)',
    });
    document.body.appendChild(notice);
  }

  function updateTele(on) {
    const btn = document.getElementById('p-tele');
    if (!btn) return;
    btn.textContent = on ? '⚡ Tele: ON' : 'Tele: OFF';
    btn.style.background = on ? '#c8f13522' : 'transparent';
    btn.style.color = on ? '#a8d420' : '#888';
    btn.style.borderColor = on ? '#c8f13555' : '#333';
  }

  // ── Auto-compress on send (Pro) ───────────────────────────────────────────
  function setupAutoCompress() {
    document.addEventListener('keydown', (e) => {
      if (!autoCompress || e.key !== 'Enter' || e.shiftKey || isCompressed) return;
      const input = getInput();
      if (!input || e.target !== input) return;
      const site = getSite();
      const text = site.getText(input);
      if (!text.trim()) return;
      if (!_quotaCache.ok) { showUpgradeNotice(_quotaCache.count); return; }
      originalText = text;
      const compressed = psEncode(text, teleMode, activePacks, customRules);
      site.setText(input, compressed);
      isCompressed = true;
      incrementQuota().then(n => {
        _quotaCache.count = n;
        if (!_quotaCache.pro && n >= FREE_LIMIT) _quotaCache.ok = false;
      });
      trackCompression(countTokens(text), countTokens(compressed), location.hostname);
    }, true);
  }

  loadProSettings();
  setupAutoCompress();

  let _obsTimer = null;
  const obs = new MutationObserver(() => {
    if (!document.getElementById('promptly-bar')) {
      clearTimeout(_obsTimer);
      _obsTimer = setTimeout(injectToolbar, 800);
    }
  });
  obs.observe(document.body, { childList: true, subtree: true });
  setTimeout(injectToolbar, 1200);
})();
