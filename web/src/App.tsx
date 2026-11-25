import {
  ApiOutlined,
  AppstoreOutlined,
  DashboardOutlined,
  ExperimentOutlined,
  FunctionOutlined,
  LineChartOutlined,
  ScheduleOutlined,
  StockOutlined,
  TransactionOutlined,
} from "@ant-design/icons";
import { Layout, Menu, Typography } from "antd";
import type { ReactNode } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";

import OverviewPage from "./pages/OverviewPage";
import PlaceholderPage from "./pages/PlaceholderPage";
import PluginsPage from "./pages/PluginsPage";

interface NavItem {
  path: string;
  label: string;
  icon: ReactNode;
  element: ReactNode;
}

const NAV: NavItem[] = [
  { path: "/", label: "概览", icon: <DashboardOutlined />, element: <OverviewPage /> },
  {
    path: "/data",
    label: "行情管理",
    icon: <StockOutlined />,
    element: (
      <PlaceholderPage
        title="行情管理"
        milestone="M1"
        description="配置数据源、同步行情、查看数据覆盖和质量、K 线查看。"
      />
    ),
  },
  {
    path: "/universe",
    label: "股票池",
    icon: <AppstoreOutlined />,
    element: (
      <PlaceholderPage
        title="股票池"
        milestone="M2"
        description="指数股票池（历史成分股）、条件筛选、历史快照。"
      />
    ),
  },
  {
    path: "/factors",
    label: "因子管理",
    icon: <FunctionOutlined />,
    element: (
      <PlaceholderPage
        title="因子管理"
        milestone="M3"
        description="因子库浏览、上传因子插件并查看验证报告、计算任务。"
      />
    ),
  },
  {
    path: "/research",
    label: "因子研究",
    icon: <ExperimentOutlined />,
    element: (
      <PlaceholderPage
        title="因子研究"
        milestone="M4"
        description="IC / RankIC、分层收益、换手、衰减、相关性、多因子对比。"
      />
    ),
  },
  {
    path: "/backtest",
    label: "回测",
    icon: <LineChartOutlined />,
    element: (
      <PlaceholderPage
        title="回测"
        milestone="M5"
        description="组合构建、调仓、成本模型；净值、回撤、持仓与交易明细。"
      />
    ),
  },
  {
    path: "/trading",
    label: "交易",
    icon: <TransactionOutlined />,
    element: (
      <PlaceholderPage
        title="交易"
        milestone="M6"
        description="模拟盘账户、持仓、委托、风控状态、紧急停止。实盘需单独开启并二次确认。"
      />
    ),
  },
  { path: "/plugins", label: "插件", icon: <ApiOutlined />, element: <PluginsPage /> },
  {
    path: "/tasks",
    label: "任务中心",
    icon: <ScheduleOutlined />,
    element: (
      <PlaceholderPage
        title="任务中心"
        milestone="M0"
        description="所有后台任务的进度、日志与失败重试。"
      />
    ),
  },
];

export default function App() {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Layout.Sider width={200} theme="light" breakpoint="lg" collapsedWidth={0}>
        <div style={{ padding: "16px 24px" }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            舵手 DuoHelm
          </Typography.Title>
        </div>
        <Menu
          mode="inline"
          selectedKeys={[pathname]}
          items={NAV.map(({ path, label, icon }) => ({ key: path, label, icon }))}
          onClick={({ key }) => navigate(key)}
        />
      </Layout.Sider>
      <Layout.Content style={{ padding: 24 }}>
        <Routes>
          {NAV.map(({ path, element }) => (
            <Route key={path} path={path} element={element} />
          ))}
          <Route path="*" element={<PlaceholderPage title="页面不存在" />} />
        </Routes>
      </Layout.Content>
    </Layout>
  );
}
