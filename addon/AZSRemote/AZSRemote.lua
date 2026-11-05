-- AZS Remote: in-game panel for the azeroth-survivors-trainer agent.
--
-- The Lua sandbox cannot reach any outside program (io/os/require are nil and
-- LaunchURL is gone inside a run), so the agent (remote.bat -> src/agent.py)
-- comes to us: it finds AZSRemote_MB in memory by its magic numbers and reads
-- and writes its slots. This file only fills in requests and shows replies.
--
-- Slot numbers and the field list must match src/addon_mailbox.py and
-- src/session.py FIELDS — tests/test_offline.py checks both.

local SLOT = { REQ = 4, CMD = 5, ARG1 = 6, ARG2 = 7, ACK = 8, RESULT = 9,
	HEARTBEAT = 10, STATE = 11, VALUES = 12, LOCKED = 33, SIZE = 53 };
local CMD = { WRITE = 1, LOCK = 2, UNLOCK = 3, HEAL = 4, UNLOCK_ALL = 5, RELOCATE = 6 };

-- kind, name, label, format ("i" whole number / "f" decimal), lockable
local FIELDS = {
	{ "stat", "hp", "最大生命", "f", true },
	{ "stat", "dmg", "伤害倍率", "f", true },
	{ "stat", "cd", "冷却倍率（越小越快）", "f", true },
	{ "stat", "crit", "暴击率", "f", true },
	{ "stat", "cm", "暴击伤害倍率", "f", true },
	{ "stat", "spd", "移动速度", "f", true },
	{ "stat", "pick", "拾取半径", "f", true },
	{ "stat", "xp", "经验倍率", "f", true },
	{ "stat", "arm", "减伤", "f", true },
	{ "stat", "reg", "每秒回血", "f", true },
	{ "stat", "area", "范围倍率", "f", true },
	{ "stat", "luck", "幸运", "f", true },
	{ "stat", "gold", "金币倍率", "f", true },
	{ "player", "gold", "金币", "i", true },
	{ "player", "hp", "当前血量", "f", true },
	{ "player", "level", "等级", "i", false },
	{ "player", "xp", "当前经验", "i", false },
	{ "player", "kills", "击杀", "i", false },
	{ "player", "rerolls", "重抽次数", "i", true },
	{ "player", "banishes", "放逐次数", "i", true },
	{ "player", "chest", "宝箱价格", "i", true },
};

-- The mailbox. Magic numbers come from tonumber() at runtime, so they exist
-- only here and not as constants of this chunk.
AZSRemote_MB = {};
for i = 1, SLOT.SIZE do
	AZSRemote_MB[i] = 0;
end
AZSRemote_MB[1] = tonumber("918273.645");
AZSRemote_MB[2] = tonumber("-556677.889");
AZSRemote_MB[3] = tonumber("1234567.125");
local MB = AZSRemote_MB;

local queue = {};
local lastBeat, lastBeatAt = 0, 0;
local waitingFor = nil;          -- request number we are waiting a reply for
local rows = {};

local function say(msg)
	if ( DEFAULT_CHAT_FRAME ) then
		DEFAULT_CHAT_FRAME:AddMessage("|cffffd100[AZS]|r " .. msg, 1, 1, 1);
	end
end

local function send(cmd, field, value)
	table.insert(queue, { cmd, field or 0, value or 0 });
end

local function fmt(f, v, name)
	if ( f == "i" ) then
		return string.format("%d", v);
	end
	if ( name == "hp" ) then
		return string.format("%.0f", v);
	end
	return string.format("%.3f", v);
end

-- --- panel -----------------------------------------------------------------
local ROW_H, TOP = 20, -58;
local panel = CreateFrame("Frame", "AZSRemoteFrame", UIParent);
panel:SetWidth(430);
panel:SetHeight(-TOP + ROW_H * (table.getn(FIELDS) + 2) + 64);
panel:SetPoint("CENTER", UIParent, "CENTER", 0, 0);
panel:SetFrameStrata("DIALOG");
panel:SetBackdrop({
	bgFile = "Interface\\DialogFrame\\UI-DialogBox-Background",
	edgeFile = "Interface\\DialogFrame\\UI-DialogBox-Border",
	tile = true, tileSize = 32, edgeSize = 32,
	insets = { left = 11, right = 12, top = 12, bottom = 11 } });
panel:EnableMouse(true);
panel:SetMovable(true);
panel:RegisterForDrag("LeftButton");
panel:SetScript("OnDragStart", function() this:StartMoving(); end);
panel:SetScript("OnDragStop", function() this:StopMovingOrSizing(); end);
panel:Hide();

local title = panel:CreateFontString(nil, "OVERLAY", "GameFontNormalLarge");
title:SetPoint("TOP", panel, "TOP", 0, -18);
title:SetText("AZS 控制器");

local status = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
status:SetPoint("TOPLEFT", panel, "TOPLEFT", 20, -40);
status:SetText("");

local close = CreateFrame("Button", "AZSRemoteClose", panel, "UIPanelCloseButton");
close:SetPoint("TOPRIGHT", panel, "TOPRIGHT", -6, -6);

local function addRow(i, y)
	local kind, name, label, f, lockable = FIELDS[i][1], FIELDS[i][2], FIELDS[i][3], FIELDS[i][4], FIELDS[i][5];
	local row = {};
	local l = panel:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall");
	l:SetPoint("TOPLEFT", panel, "TOPLEFT", 22, y);
	l:SetText(label);
	row.cur = panel:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall");
	row.cur:SetPoint("TOPRIGHT", panel, "TOPLEFT", 250, y);
	row.cur:SetJustifyH("RIGHT");
	row.cur:SetText("-");

	local eb = CreateFrame("EditBox", "AZSRemoteEdit" .. i, panel, "InputBoxTemplate");
	eb:SetWidth(64);
	eb:SetHeight(18);
	eb:SetPoint("TOPLEFT", panel, "TOPLEFT", 264, y + 3);
	eb:SetAutoFocus(false);
	eb:SetScript("OnEscapePressed", function() this:ClearFocus(); end);
	row.edit = eb;

	local function value()
		local v = tonumber(eb:GetText());
		if ( not v ) then
			v = MB[SLOT.VALUES + i - 1];
			eb:SetText(fmt(f, v, name));
		end
		return v;
	end

	if ( kind == "player" ) then
		local b = CreateFrame("Button", "AZSRemoteWrite" .. i, panel, "UIPanelButtonTemplate");
		b:SetWidth(36);
		b:SetHeight(18);
		b:SetPoint("TOPLEFT", panel, "TOPLEFT", 332, y + 3);
		b:SetText("写");
		b:SetScript("OnClick", function() send(CMD.WRITE, i, value()); end);
	end
	if ( lockable ) then
		local c = CreateFrame("CheckButton", "AZSRemoteLock" .. i, panel, "UICheckButtonTemplate");
		c:SetWidth(22);
		c:SetHeight(22);
		c:SetPoint("TOPLEFT", panel, "TOPLEFT", 372, y + 5);
		local t = getglobal(c:GetName() .. "Text");
		if ( t ) then
			t:SetText("锁");
		end
		c:SetScript("OnClick", function()
			if ( this:GetChecked() ) then
				send(CMD.LOCK, i, value());
			else
				send(CMD.UNLOCK, i, 0);
			end
		end);
		row.lock = c;
	end

	eb:SetScript("OnEnterPressed", function()
		this:ClearFocus();
		if ( kind == "stat" ) then          -- the game recomputes stats: Enter locks
			if ( row.lock ) then
				row.lock:SetChecked(1);
			end
			send(CMD.LOCK, i, value());
		else
			send(CMD.WRITE, i, value());
		end
	end);
	rows[i] = row;
end

local y = TOP;
for i = 1, table.getn(FIELDS) do
	if ( i == 14 ) then
		y = y - 6;                          -- a gap between stats and run state
	end
	addRow(i, y);
	y = y - ROW_H;
end

local function button(name, text, x, onclick)
	local b = CreateFrame("Button", name, panel, "UIPanelButtonTemplate");
	b:SetWidth(96);
	b:SetHeight(22);
	b:SetPoint("BOTTOMLEFT", panel, "BOTTOMLEFT", x, 18);
	b:SetText(text);
	b:SetScript("OnClick", onclick);
end
button("AZSRemoteHeal", "回满血", 22, function() send(CMD.HEAL); end);
button("AZSRemoteUnlock", "全部解锁", 124, function() send(CMD.UNLOCK_ALL); end);
button("AZSRemoteRelocate", "重新定位", 226, function() send(CMD.RELOCATE); end);

-- --- talking to the agent ----------------------------------------------------
local RESULT_TEXT = { [0] = "完成", [1] = "后台还在定位，稍等", [2] = "输入有误", [3] = "执行失败（看 remote.bat 窗口）" };
local STATE_TEXT = { [1] = "定位中…", [2] = "已就绪", [3] = "目标失效：开新的一局或点“重新定位”" };

local poll = 0;
local driver = CreateFrame("Frame");
driver:SetScript("OnUpdate", function()
	poll = poll + (arg1 or 0);
	if ( poll < 0.1 ) then
		return;
	end
	poll = 0;
	local now = GetTime();
	if ( MB[SLOT.HEARTBEAT] ~= lastBeat ) then
		lastBeat, lastBeatAt = MB[SLOT.HEARTBEAT], now;
	end
	local connected = now - lastBeatAt < 2 and lastBeat ~= 0;

	-- a reply to our last request?
	if ( waitingFor and MB[SLOT.ACK] == waitingFor ) then
		local r = MB[SLOT.RESULT];
		if ( r ~= 0 ) then
			say(RESULT_TEXT[r] or ("result " .. r));
		end
		waitingFor = nil;
	end
	-- post the next one, one at a time
	if ( not waitingFor and table.getn(queue) > 0 and MB[SLOT.ACK] == MB[SLOT.REQ] ) then
		local q = table.remove(queue, 1);
		MB[SLOT.CMD], MB[SLOT.ARG1], MB[SLOT.ARG2] = q[1], q[2], q[3];
		MB[SLOT.REQ] = MB[SLOT.REQ] + 1;     -- last: the agent acts when this changes
		waitingFor = MB[SLOT.REQ];
	end

	if ( not panel:IsVisible() ) then
		return;
	end
	if ( not connected ) then
		status:SetText("|cffff5555后台未连接：先双击仓库里的 remote.bat|r");
	else
		local st = MB[SLOT.STATE];
		local text = STATE_TEXT[st] or "";
		if ( table.getn(queue) > 0 or waitingFor ) then
			text = text .. "  ·  处理中…";
		end
		status:SetText((st == 2 and "|cff55ff55" or "|cffffd100") .. text .. "|r");
	end
	if ( connected and MB[SLOT.STATE] == 2 ) then
		local busy = waitingFor or table.getn(queue) > 0;   -- don't undo a fresh click
		for i = 1, table.getn(FIELDS) do
			local row = rows[i];
			row.cur:SetText(fmt(FIELDS[i][4], MB[SLOT.VALUES + i - 1], FIELDS[i][2]));
			if ( row.lock and not busy ) then
				row.lock:SetChecked(MB[SLOT.LOCKED + i - 1] == 1 and 1 or nil);
			end
		end
	end
end);

SLASH_AZSREMOTE1 = "/azs";
SlashCmdList["AZSREMOTE"] = function()
	if ( panel:IsVisible() ) then
		panel:Hide();
	else
		panel:Show();
	end
end;

local hello = CreateFrame("Frame");
hello:RegisterEvent("PLAYER_ENTERING_WORLD");
hello:SetScript("OnEvent", function()
	say("控制器已加载：输入 /azs 打开（需要先运行 remote.bat）");
	this:UnregisterEvent("PLAYER_ENTERING_WORLD");
end);
