const pptxgen = require("pptxgenjs");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const { FaClipboardList, FaIndustry, FaWarehouse, FaExchangeAlt, FaCheckCircle, FaChartLine, FaCubes, FaCog, FaUsers, FaEnvelope, FaSearch, FaGlobe, FaFileExport, FaSignInAlt, FaLayerGroup, FaRocket } = require("react-icons/fa");

function renderIconSvg(IconComponent, color = "#000000", size = 256) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color, size: String(size) })
  );
}

async function iconToBase64Png(IconComponent, color, size = 256) {
  const svg = renderIconSvg(IconComponent, color, size);
  const pngBuffer = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + pngBuffer.toString("base64");
}

// Color palette: Midnight Executive with NAI blue accent
const C = {
  navy: "1A237E",
  blue: "1A73E8",
  lightBlue: "E3F2FD",
  accent: "00BCD4",
  white: "FFFFFF",
  offWhite: "F8F9FA",
  dark: "1E293B",
  gray: "64748B",
  lightGray: "E2E8F0",
  green: "10B981",
  orange: "F59E0B",
  red: "EF4444",
  teal: "0D9488",
  text: "334155",
  cardBg: "FFFFFF",
};

const F = { header: "Arial Black", body: "Arial" };

async function createPPT() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "Material Kanban System";
  pres.title = "物料领取看板系统 - 培训材料";

  // ===================== ICONS =====================
  const icons = {};
  const iconList = [
    ["clipboard", FaClipboardList, C.blue],
    ["industry", FaIndustry, C.white],
    ["warehouse", FaWarehouse, C.white],
    ["exchange", FaExchangeAlt, C.green],
    ["check", FaCheckCircle, C.green],
    ["chart", FaChartLine, C.accent],
    ["cubes", FaCubes, C.orange],
    ["cog", FaCog, C.gray],
    ["users", FaUsers, C.blue],
    ["envelope", FaEnvelope, C.accent],
    ["search", FaSearch, C.gray],
    ["globe", FaGlobe, C.accent],
    ["export", FaFileExport, C.green],
    ["login", FaSignInAlt, C.blue],
    ["layers", FaLayerGroup, C.accent],
    ["rocket", FaRocket, C.orange],
  ];
  for (const [name, comp, color] of iconList) {
    icons[name] = await iconToBase64Png(comp, "#" + color);
  }

  // ===================== HELPER =====================
  function addSlideWithHeader(title, headerColor = C.navy) {
    const slide = pres.addSlide();
    slide.background = { color: C.offWhite };
    // Top bar
    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 0.9, fill: { color: headerColor } });
    slide.addImage({ data: icons.clipboard, x: 0.25, y: 0.15, w: 0.5, h: 0.6 });
    slide.addText(title, { x: 0.9, y: 0.15, w: 8.5, h: 0.6, fontSize: 22, fontFace: F.header, color: C.white, valign: "middle", margin: 0 });
    // bottom line
    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.425, w: 10, h: 0.2, fill: { color: headerColor } });
    // page number placeholder
    return slide;
  }

  // ===================== SLIDE 1: TITLE =====================
  {
    const slide = pres.addSlide();
    slide.background = { color: C.navy };
    // Decorative shape
    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 2.8, fill: { color: "0D47A1" } });
    // Logo icon
    slide.addImage({ data: icons.clipboard, x: 4.5, y: 0.6, w: 1, h: 1.2 });
    // Title
    slide.addText("物料领取看板系统", { x: 0.5, y: 1.8, w: 9, h: 0.8, fontSize: 40, fontFace: F.header, color: C.white, align: "center", margin: 0 });
    slide.addText("Material Pickup Kanban System", { x: 0.5, y: 2.5, w: 9, h: 0.5, fontSize: 18, fontFace: F.body, color: C.lightBlue, align: "center", margin: 0 });
    slide.addText("生产物料补充闭环管理培训", { x: 0.5, y: 3.2, w: 9, h: 0.6, fontSize: 22, fontFace: F.body, color: C.accent, align: "center", margin: 0 });
    // Bottom info
    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.125, w: 10, h: 0.5, fill: { color: "0D47A1" } });
    slide.addText("NAI Group | 物料管理数字化转型", { x: 0.5, y: 5.15, w: 9, h: 0.4, fontSize: 12, fontFace: F.body, color: C.lightGray, align: "center", margin: 0 });
  }

  // ===================== SLIDE 2: AGENDA =====================
  {
    const slide = addSlideWithHeader("目录 / Agenda");
    const items = [
      { num: "01", title: "系统开发背景", sub: "为什么需要这个系统？" },
      { num: "02", title: "物料补充闭环管理", sub: "如何实现端到端闭环？" },
      { num: "03", title: "系统功能介绍", sub: "核心功能模块详解" },
      { num: "04", title: "两种发料方式对比", sub: "按需物料 vs 最小包装" },
      { num: "05", title: "操作流程演示", sub: "如何上手使用" },
    ];
    items.forEach((item, i) => {
      const yBase = 1.3 + i * 0.85;
      slide.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: yBase, w: 9, h: 0.7, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });
      slide.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: yBase, w: 0.06, h: 0.7, fill: { color: C.blue } });
      slide.addText(item.num, { x: 0.8, y: yBase, w: 0.7, h: 0.7, fontSize: 22, fontFace: F.header, color: C.blue, valign: "middle", margin: 0 });
      slide.addText(item.title, { x: 1.6, y: yBase + 0.05, w: 5, h: 0.4, fontSize: 16, fontFace: F.body, color: C.dark, bold: true, valign: "middle", margin: 0 });
      slide.addText(item.sub, { x: 1.6, y: yBase + 0.38, w: 5, h: 0.3, fontSize: 11, fontFace: F.body, color: C.gray, valign: "middle", margin: 0 });
      slide.addImage({ data: icons.check, x: 8.8, y: yBase + 0.15, w: 0.4, h: 0.4 });
    });
  }

  // ===================== SLIDE 3: BACKGROUND =====================
  {
    const slide = addSlideWithHeader("系统开发背景", C.blue);
    slide.addText("为什么需要物料领取看板系统？", { x: 0.5, y: 1.2, w: 9, h: 0.4, fontSize: 16, fontFace: F.body, color: C.gray, margin: 0 });

    const items = [
      { icon: icons.industry, title: "传统模式痛点", desc: "生产现场物料领取依赖纸质单据和口头沟通，信息传递慢、易出错，缺乏系统化跟踪" },
      { icon: icons.warehouse, title: "仓库管理被动", desc: "仓库无法提前知道备料需求，只能被动响应，备料效率低，影响生产进度" },
      { icon: icons.exchange, title: "信息断点", desc: "领料人、审批人、仓库之间的信息割裂，审批状态不透明，历史追溯困难" },
    ];
    items.forEach((item, i) => {
      const xBase = 0.4 + i * 3.1;
      slide.addShape(pres.shapes.RECTANGLE, { x: xBase, y: 1.8, w: 3, h: 3.2, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 4, offset: 1, color: "000000", opacity: 0.1 } });
      slide.addShape(pres.shapes.RECTANGLE, { x: xBase, y: 1.8, w: 3, h: 0.06, fill: { color: C.blue } });
      slide.addImage({ data: item.icon, x: xBase + 1.15, y: 2.1, w: 0.7, h: 0.7 });
      slide.addText(item.title, { x: xBase + 0.2, y: 2.9, w: 2.6, h: 0.4, fontSize: 14, fontFace: F.body, color: C.dark, bold: true, align: "center", margin: 0 });
      slide.addText(item.desc, { x: xBase + 0.2, y: 3.4, w: 2.6, h: 1.4, fontSize: 11, fontFace: F.body, color: C.text, align: "center", margin: 0 });
    });
  }

  // ===================== SLIDE 4: CLOSED-LOOP MANAGEMENT =====================
  {
    const slide = addSlideWithHeader("物料补充闭环管理", C.teal);
    slide.addText("从需求提出到物料发放的完整闭环流程", { x: 0.5, y: 1.2, w: 9, h: 0.4, fontSize: 14, fontFace: F.body, color: C.gray, margin: 0 });

    const steps = [
      { title: "创建申请", desc: "领料员在线提交物料申请，关联工单和物料信息", icon: icons.login },
      { title: "主管审批", desc: "主管线上审批确认，系统自动发送邮件通知", icon: icons.envelope },
      { title: "仓库备料", desc: "仓库人员接收任务，按订单备料、分配批次", icon: icons.warehouse },
      { title: "领取确认", desc: "领料员到仓库签字确认领料，完成闭环", icon: icons.check },
    ];
    // Arrow connectors
    for (let i = 0; i < steps.length - 1; i++) {
      const x = 1.2 + i * 2.3;
      slide.addText("→", { x: x + 1.6, y: 2.0, w: 0.6, h: 0.5, fontSize: 28, color: C.accent, align: "center", valign: "middle", margin: 0 });
    }
    steps.forEach((s, i) => {
      const x = 0.3 + i * 2.5;
      slide.addShape(pres.shapes.RECTANGLE, { x, y: 1.8, w: 2.2, h: 3.2, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });
      slide.addShape(pres.shapes.RECTANGLE, { x, y: 1.8, w: 2.2, h: 0.06, fill: { color: C.teal } });
      slide.addImage({ data: s.icon, x: x + 0.75, y: 2.1, w: 0.7, h: 0.7 });
      slide.addText(s.title, { x: x + 0.1, y: 2.9, w: 2, h: 0.4, fontSize: 14, fontFace: F.body, color: C.dark, bold: true, align: "center", margin: 0 });
      slide.addText(s.desc, { x: x + 0.1, y: 3.4, w: 2, h: 1.3, fontSize: 11, fontFace: F.body, color: C.text, align: "center", margin: 0 });
    });

    // Bottom highlight
    slide.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: 5.1, w: 9, h: 0.15, fill: { color: C.green } });
  }

  // ===================== SLIDE 5: SYSTEM FUNCTIONS =====================
  {
    const slide = addSlideWithHeader("核心功能介绍", C.blue);
    const features = [
      { icon: icons.clipboard, title: "看板视图", desc: "实时状态看板，按状态分组显示所有工单，一目了然" },
      { icon: icons.cubes, title: "双模申请", desc: "支持按需物料申请和最小包装申请两种模式" },
      { icon: icons.search, title: "物料验证", desc: "对接CSI/ERP系统，实时验证工单和物料有效性" },
      { icon: icons.envelope, title: "邮件审批", desc: "审批人通过邮件即可完成审批，无需登录系统" },
      { icon: icons.warehouse, title: "仓库管理", desc: "备料任务管理、批次号分配、缺料登记全流程支持" },
      { icon: icons.chart, title: "记录查询", desc: "历史记录检索、CSV导出，方便数据追溯和分析" },
    ];
    features.forEach((f, i) => {
      const col = i % 3;
      const row = Math.floor(i / 3);
      const x = 0.3 + col * 3.2;
      const y = 1.2 + row * 2.0;
      slide.addShape(pres.shapes.RECTANGLE, { x, y, w: 3.05, h: 1.7, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });
      slide.addImage({ data: f.icon, x: x + 0.15, y: y + 0.15, w: 0.5, h: 0.5 });
      slide.addText(f.title, { x: x + 0.8, y: y + 0.15, w: 2.1, h: 0.4, fontSize: 14, fontFace: F.body, color: C.dark, bold: true, valign: "middle", margin: 0 });
      slide.addText(f.desc, { x: x + 0.15, y: y + 0.7, w: 2.75, h: 0.85, fontSize: 11, fontFace: F.body, color: C.text, margin: 0 });
    });
  }

  // ===================== SLIDE 6: SYSTEM FEATURES DETAIL =====================
  {
    const slide = addSlideWithHeader("系统亮点功能", C.accent);
    const items = [
      { icon: icons.globe, title: "中英文双语", desc: "系统支持中英文界面一键切换，适应国际化团队" },
      { icon: icons.login, title: "AD域认证", desc: "集成公司LDAP/AD认证，无需维护独立账号密码" },
      { icon: icons.export, title: "FIFO批次管理", desc: "系统自动按先进先出原则推荐批次号，提升库存周转" },
      { icon: icons.cog, title: "急料优先", desc: "支持标记急料，看板自动排序，急料申请优先展示" },
    ];
    items.forEach((item, i) => {
      const y = 1.3 + i * 1.05;
      slide.addShape(pres.shapes.RECTANGLE, { x: 0.5, y, w: 9, h: 0.85, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 2, offset: 1, color: "000000", opacity: 0.06 } });
      slide.addShape(pres.shapes.RECTANGLE, { x: 0.5, y, w: 0.06, h: 0.85, fill: { color: C.accent } });
      slide.addImage({ data: item.icon, x: 0.8, y: y + 0.15, w: 0.5, h: 0.5 });
      slide.addText(item.title, { x: 1.5, y: y + 0.05, w: 3, h: 0.4, fontSize: 14, fontFace: F.body, color: C.dark, bold: true, valign: "middle", margin: 0 });
      slide.addText(item.desc, { x: 1.5, y: y + 0.42, w: 7.5, h: 0.35, fontSize: 11, fontFace: F.body, color: C.text, valign: "middle", margin: 0 });
    });
  }

  // ===================== SLIDE 7: NORMAL vs MIN PACK =====================
  {
    const slide = addSlideWithHeader("发料方式对比", C.orange);
    slide.addText("按需物料申请  vs  最小包装发料", { x: 0.5, y: 1.2, w: 9, h: 0.4, fontSize: 16, fontFace: F.body, color: C.gray, align: "center", margin: 0 });

    // Column headers
    const colW = 4.2;
    const gap = 0.3;
    // Normal
    {
      const x = 0.5;
      slide.addShape(pres.shapes.RECTANGLE, { x, y: 1.7, w: colW, h: 0.55, fill: { color: C.blue } });
      slide.addText("按需物料申请", { x, y: 1.7, w: colW, h: 0.55, fontSize: 15, fontFace: F.header, color: C.white, align: "center", valign: "middle", margin: 0 });
      slide.addShape(pres.shapes.RECTANGLE, { x: x + 0.1, y: 2.4, w: colW - 0.2, h: 2.7, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });

      const normalFeatures = [
        { label: "工单关联", val: "必须关联生产工单" },
        { label: "审批流程", val: "主管审批 → 才能备料" },
        { label: "补料原因", val: "支持报废/不良/来料不足" },
        { label: "适用场景", val: "生产线按需补充物料" },
      ];
      normalFeatures.forEach((f, i) => {
        const fy = 2.5 + i * 0.6;
        slide.addText(f.label, { x: x + 0.2, y: fy, w: 1.5, h: 0.25, fontSize: 10, fontFace: F.body, color: C.gray, margin: 0 });
        slide.addText(f.val, { x: x + 0.2, y: fy + 0.25, w: 3.4, h: 0.25, fontSize: 11, fontFace: F.body, color: C.dark, bold: true, margin: 0 });
        if (i < 3) slide.addShape(pres.shapes.LINE, { x: x + 0.2, y: fy + 0.52, w: colW - 0.6, h: 0, line: { color: C.lightGray, width: 0.5 } });
      });
    }

    // Min Pack
    {
      const x = 0.5 + colW + gap;
      slide.addShape(pres.shapes.RECTANGLE, { x, y: 1.7, w: colW, h: 0.55, fill: { color: C.green } });
      slide.addText("最小包装发料", { x, y: 1.7, w: colW, h: 0.55, fontSize: 15, fontFace: F.header, color: C.white, align: "center", valign: "middle", margin: 0 });
      slide.addShape(pres.shapes.RECTANGLE, { x: x + 0.1, y: 2.4, w: colW - 0.2, h: 2.7, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });

      const mpFeatures = [
        { label: "工单关联", val: "无需关联工单" },
        { label: "审批流程", val: "无需审批，直达备料" },
        { label: "发料标记", val: "物料Backflush=1（最小包装）" },
        { label: "适用场景", val: "通用物料、最小包装发放" },
      ];
      mpFeatures.forEach((f, i) => {
        const fy = 2.5 + i * 0.6;
        slide.addText(f.label, { x: x + 0.2, y: fy, w: 1.5, h: 0.25, fontSize: 10, fontFace: F.body, color: C.gray, margin: 0 });
        slide.addText(f.val, { x: x + 0.2, y: fy + 0.25, w: 3.4, h: 0.25, fontSize: 11, fontFace: F.body, color: C.dark, bold: true, margin: 0 });
        if (i < 3) slide.addShape(pres.shapes.LINE, { x: x + 0.2, y: fy + 0.52, w: colW - 0.6, h: 0, line: { color: C.lightGray, width: 0.5 } });
      });
    }

    // Center VS badge
    slide.addShape(pres.shapes.OVAL, { x: 4.7, y: 3.3, w: 0.6, h: 0.6, fill: { color: C.orange } });
    slide.addText("VS", { x: 4.7, y: 3.3, w: 0.6, h: 0.6, fontSize: 12, fontFace: F.header, color: C.white, align: "center", valign: "middle", margin: 0 });
  }

  // ===================== SLIDE 8: WORKFLOW =====================
  {
    const slide = addSlideWithHeader("操作流程概览", C.green);
    const steps = [
      { title: "1. 登录系统", desc: "使用AD域账号登录", icon: icons.login },
      { title: "2. 新建申请", desc: "选择按需或最小包装", icon: icons.cubes },
      { title: "3. 填写物料", desc: "输入工单号和物料信息", icon: icons.search },
      { title: "4. 提交等待", desc: "提交流程，等待审批/备料", icon: icons.rocket },
    ];
    steps.forEach((s, i) => {
      const x = 0.3 + i * 2.5;
      slide.addShape(pres.shapes.RECTANGLE, { x, y: 1.4, w: 2.2, h: 2.5, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });
      slide.addShape(pres.shapes.RECTANGLE, { x, y: 1.4, w: 2.2, h: 0.06, fill: { color: C.green } });
      slide.addImage({ data: s.icon, x: x + 0.75, y: 1.6, w: 0.7, h: 0.7 });
      slide.addText(s.title, { x: x + 0.1, y: 2.4, w: 2, h: 0.35, fontSize: 13, fontFace: F.body, color: C.dark, bold: true, align: "center", margin: 0 });
      slide.addText(s.desc, { x: x + 0.1, y: 2.8, w: 2, h: 0.8, fontSize: 11, fontFace: F.body, color: C.text, align: "center", margin: 0 });
    });

    // Visual difference callout
    slide.addShape(pres.shapes.RECTANGLE, { x: 0.5, y: 4.2, w: 9, h: 0.9, fill: { color: C.lightBlue } });
    slide.addText("关键区别", { x: 0.7, y: 4.25, w: 1.5, h: 0.3, fontSize: 12, fontFace: F.body, color: C.blue, bold: true, margin: 0 });
    slide.addText("按需物料：需主管审批 → 主管邮箱审批通过 → 仓库备料\n最小包装：提交即进入待备料状态 → 仓库直接备料", { x: 0.7, y: 4.55, w: 8.5, h: 0.5, fontSize: 11, fontFace: F.body, color: C.text, margin: 0 });
  }

  // ===================== SLIDE 9: BENEFITS =====================
  {
    const slide = addSlideWithHeader("系统收益", C.teal);
    const benefits = [
      { icon: icons.check, title: "流程标准化", desc: "物料领取全过程数字化、规范化，告别纸质单据" },
      { icon: icons.chart, title: "效率提升", desc: "审批在线化、备料任务化，减少等待时间" },
      { icon: icons.search, title: "全程追踪", desc: "每一步操作都有日志记录，责任清晰可追溯" },
      { icon: icons.warehouse, title: "库存优化", desc: "FIFO批次管理帮助提高库存周转效率" },
    ];
    benefits.forEach((b, i) => {
      const col = i % 2;
      const row = Math.floor(i / 2);
      const x = 0.5 + col * 4.5;
      const y = 1.3 + row * 1.8;
      slide.addShape(pres.shapes.RECTANGLE, { x, y, w: 4.2, h: 1.5, fill: { color: C.cardBg }, shadow: { type: "outer", blur: 3, offset: 1, color: "000000", opacity: 0.08 } });
      slide.addShape(pres.shapes.RECTANGLE, { x, y, w: 0.06, h: 1.5, fill: { color: C.teal } });
      slide.addImage({ data: b.icon, x: x + 0.3, y: y + 0.4, w: 0.6, h: 0.6 });
      slide.addText(b.title, { x: x + 1.1, y: y + 0.2, w: 2.8, h: 0.4, fontSize: 16, fontFace: F.body, color: C.dark, bold: true, valign: "middle", margin: 0 });
      slide.addText(b.desc, { x: x + 1.1, y: y + 0.7, w: 2.8, h: 0.6, fontSize: 11, fontFace: F.body, color: C.text, margin: 0 });
    });
  }

  // ===================== SLIDE 10: Q&A =====================
  {
    const slide = pres.addSlide();
    slide.background = { color: C.navy };
    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 0, w: 10, h: 2.5, fill: { color: "0D47A1" } });
    slide.addImage({ data: icons.users, x: 4.5, y: 0.8, w: 1, h: 1 });
    slide.addText("Questions & Answers", { x: 0.5, y: 2.0, w: 9, h: 0.8, fontSize: 36, fontFace: F.header, color: C.white, align: "center", margin: 0 });
    slide.addText("感谢聆听", { x: 0.5, y: 3.2, w: 9, h: 0.6, fontSize: 24, fontFace: F.body, color: C.accent, align: "center", margin: 0 });

    slide.addShape(pres.shapes.RECTANGLE, { x: 0, y: 5.125, w: 10, h: 0.5, fill: { color: "0D47A1" } });
    slide.addText("NAI Group | 物料领取看板系统", { x: 0.5, y: 5.15, w: 9, h: 0.4, fontSize: 12, fontFace: F.body, color: C.lightGray, align: "center", margin: 0 });
  }

  // ===================== SAVE =====================
  const outPath = "D:/Workbuddy/多智能体/物料领取看板/物料领取看板系统培训.pptx";
  await pres.writeFile({ fileName: outPath });
  console.log("PPT created: " + outPath);
}

createPPT().catch(e => console.error(e));
