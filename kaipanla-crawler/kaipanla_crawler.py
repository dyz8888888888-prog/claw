# -*- coding: utf-8 -*-
"""
开盘啦APP数据爬虫

主要功能：
- get_daily_data(end_date, start_date=None): 获取指定日期范围的交易数据
  - 只传end_date: 返回单日Series
  - 传start_date和end_date: 返回日期范围DataFrame
- get_new_high_data(end_date, start_date=None): 获取百日新高数据
- get_sector_intraday(sector_code, date=None): 获取板块分时数据
- get_stock_intraday(stock_code, date=None): 获取个股分时数据
- get_abnormal_stocks(): 获取异动个股数据（实时）

- get_sector_ranking(date, index): 获取涨停原因板块数据
- get_sector_strength(sector_code, date=None): 获取板块强度数据
- get_multiple_sectors_strength(sector_codes, date=None): 批量获取多个板块的强度数据
- get_sector_strength_history(sector_code, start_date, end_date): 获取板块强度历史数据
- get_sector_strength_dataframe(sector_code, start_date, end_date): 获取板块强度历史DataFrame
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
import uuid
import urllib3
import time

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def is_weekend(date_str):
    """判断给定日期是否为周末"""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return date_obj.weekday() >= 5
    except:
        return False

def get_trading_dates(start_date, end_date):
    """获取指定日期范围内的所有交易日（排除周末）"""
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    if start > end:
        start, end = end, start
    trading_dates = []
    current = start
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        if not is_weekend(date_str):
            trading_dates.append(date_str)
        current += timedelta(days=1)
    return trading_dates

class KaipanlaCrawler:
    """开盘啦数据爬虫"""

    def __init__(self):
        self.base_url = "https://apphis.longhuvip.com/w1/api/index.php"
        self.sector_base_url = "https://apphwhq.longhuvip.com/w1/api/index.php"
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SHARK PRS-A0 Build/PQ3A.190605.01141736)",
            "Host": "apphis.longhuvip.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }
        self.sector_headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SHARK PRS-A0 Build/PQ3A.190605.01141736)",
            "Host": "apphwhq.longhuvip.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
        }

    def _request(self, data_params, date, timeout=None):
        """发送POST请求"""
        params = {"apiv": "w42", "PhoneOSNew": "1", "VerSion": "5.21.0.2"}
        data = {
            "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()),
            "VerSion": "5.21.0.2",
            "apiv": "w42",
            "Day": date
        }
        data.update(data_params)
        try:
            response = requests.post(
                self.base_url, params=params, data=data,
                headers=self.headers, verify=False,
                proxies={'http': None, 'https': None},
                timeout=timeout
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"请求失败 ({date}): {e}")
            return {}

    def _get_single_day_data(self, date):
        """获取单日完整数据"""
        result1 = self._request({"a": "HisZhangFuDetail", "c": "HisHomeDingPan"}, date)
        info1 = result1.get("info", {}) if result1 else {}

        result2 = self._request({"a": "GetZsReal", "c": "StockL2History"}, date)
        stock_list = result2.get("StockList", []) if result2 else []
        sh_index = None
        for stock in stock_list:
            if stock.get("StockID") == "SH000001":
                sh_index = stock
                break

        result3 = self._request({"a": "ZhangTingExpression", "c": "HisHomeDingPan"}, date)
        info3 = result3.get("info", []) if result3 else []

        result4 = self._request({"a": "SharpWithdrawal", "c": "HisHomeDingPan"}, date)
        withdrawal_num = result4.get("num", 0) if result4 else 0

        data = {
            "日期": result1.get("date", date) if result1 else date,
            "涨停数": int(info1.get("ZT", 0)),
            "实际涨停": int(info1.get("SJZT", 0)),
            "跌停数": int(info1.get("DT", 0)),
            "实际跌停": int(info1.get("SJDT", 0)),
            "上涨家数": int(info1.get("SZJS", 0)),
            "下跌家数": int(info1.get("XDJS", 0)),
            "平盘家数": int(info1.get("0", 0)),
            "上证指数": float(sh_index.get("last_px", 0)) if sh_index else 0,
            "最新价": float(sh_index.get("last_px", 0)) if sh_index else 0,
            "涨跌幅": sh_index.get("increase_rate", "") if sh_index else "",
            "成交额": int(sh_index.get("turnover", 0)) if sh_index else 0,
            "首板数量": info3[0] if len(info3) > 0 else 0,
            "2连板数量": info3[1] if len(info3) > 1 else 0,
            "3连板数量": info3[2] if len(info3) > 2 else 0,
            "4连板以上数量": info3[3] if len(info3) > 3 else 0,
            "连板率": round(info3[4], 2) if len(info3) > 4 else 0,
            "大幅回撤家数": withdrawal_num,
        }
        return data

    def get_daily_data(self, end_date, start_date=None):
        """获取交易日数据"""
        if start_date is None:
            data = self._get_single_day_data(end_date)
            return pd.Series(data)

        date_list = get_trading_dates(start_date, end_date)
        print(f"日期范围: {start_date} 到 {end_date}")
        print(f"交易日数量: {len(date_list)} 天")

        records = []
        for date in date_list:
            print(f"正在获取 {date} 的数据...")
            data = self._get_single_day_data(date)
            records.append(data)

        df = pd.DataFrame(records)
        df = df[df["涨停数"] > 0]
        return df

    def get_new_high_data(self, end_date, start_date=None, timeout=None):
        """获取百日新高数据"""
        data = {
            "a": "GetDayNewHigh_W28", "st": "360", "c": "StockNewHigh",
            "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()),
            "VerSion": "5.21.0.2", "Index": "0", "GroupID": "ALL",
            "apiv": "w42", "Type": "0_0_0_0_0"
        }
        try:
            response = requests.post(self.base_url, data=data, headers=self.headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return pd.Series()
            x_data = result.get("x", [])
            if not x_data:
                return pd.Series()
            dates, new_highs = [], []
            for item in x_data:
                parts = item.split("_")
                if len(parts) >= 3:
                    date_str = parts[0]
                    new_count = int(parts[2])
                    formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                    dates.append(formatted_date)
                    new_highs.append(new_count)
            series = pd.Series(new_highs, index=dates)
            series.index.name = "日期"
            series.name = "今日新增"
            if start_date is None:
                return series[end_date] if end_date in series.index else pd.Series()
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            if start > end:
                start, end = end, start
            mask = (pd.to_datetime(series.index) >= start) & (pd.to_datetime(series.index) <= end)
            return series[mask]
        except Exception as e:
            print(f"请求新高数据失败: {e}")
            return pd.Series()

    def get_market_sentiment(self, date=None):
        """获取涨跌统计数据"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        result = self._request({"a": "HisZhangFuDetail", "c": "HisHomeDingPan"}, date)
        if not result:
            return pd.DataFrame()
        info = result.get("info", {})
        return pd.DataFrame({
            "日期": [result.get("date", date)],
            "涨停数": [int(info.get("ZT", 0))],
            "实际涨停": [int(info.get("SJZT", 0))],
            "跌停数": [int(info.get("DT", 0))],
            "实际跌停": [int(info.get("SJDT", 0))],
            "上涨家数": [int(info.get("SZJS", 0))],
            "下跌家数": [int(info.get("XDJS", 0))],
            "平盘家数": [int(info.get("0", 0))]
        })

    def get_market_index(self, date=None):
        """获取大盘指数数据"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        result = self._request({"a": "GetZsReal", "c": "StockL2History"}, date)
        if not result:
            return pd.DataFrame()
        return pd.DataFrame([{
            "日期": date, "指数代码": s.get("StockID", ""),
            "指数名称": s.get("prod_name", ""),
            "最新价": float(s.get("last_px", 0)),
            "涨跌额": float(s.get("increase_amount", 0)),
            "涨跌幅": s.get("increase_rate", ""),
            "成交额(元)": int(s.get("turnover", 0))
        } for s in result.get("StockList", [])])

    def get_limit_up_ladder(self, date=None):
        """获取连板梯队数据"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        result = self._request({"a": "ZhangTingExpression", "c": "HisHomeDingPan"}, date)
        if not result:
            return pd.DataFrame()
        info = result.get("info", [])
        if len(info) < 12:
            return pd.DataFrame()
        return pd.DataFrame({
            "日期": [date], "一板": [info[0]], "二板": [info[1]],
            "三板": [info[2]], "高度板": [info[3]],
            "连板率(%)": [round(info[4], 2)],
            "昨日首板今日上涨数": [info[5]], "昨日首板今日下跌数": [info[6]],
            "今日涨停破板率(%)": [round(info[7], 2)],
            "昨日涨停今表现(%)": [round(info[8], 2)],
            "昨日连板今表现(%)": [round(info[9], 2)],
            "昨日破板今表现(%)": [round(info[10], 2)],
            "市场评价": [info[11]]
        })

    def get_sharp_withdrawal(self, date=None):
        """获取大幅回撤股票数据"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        result = self._request({"a": "SharpWithdrawal", "c": "HisHomeDingPan"}, date)
        if not result:
            return pd.DataFrame()
        total_num = result.get("num", 0)
        return pd.DataFrame([{
            "日期": result.get("date", date), "股票代码": i[0],
            "股票名称": i[1], "当日涨跌幅(%)": round(i[2], 2),
            "回撤幅度(%)": round(i[3], 2), "最新价": round(i[4], 2),
            "总数": total_num
        } for i in result.get("info", []) if len(i) >= 5])

    def get_sector_ranking(self, date=None, index=0, timeout=None):
        """获取涨停原因板块数据"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        data = {
            "a": "GetPlateInfo_w38", "st": "100", "c": "DailyLimitResumption",
            "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()),
            "VerSion": "5.21.0.2", "Index": str(index), "apiv": "w42", "Day": date
        }
        try:
            response = requests.post(self.sector_base_url, data=data, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                print(f"获取板块数据失败: {result.get('errcode', 'unknown error')}")
                return {"summary": {}, "sectors": []}
            nums = result.get("nums", {})
            summary = {
                "日期": result.get("date", date), "上涨家数": nums.get("SZJS", 0),
                "下跌家数": nums.get("XDJS", 0), "涨停数": nums.get("ZT", 0),
                "跌停数": nums.get("DT", 0), "涨跌比": round(nums.get("ZBL", 0), 2),
                "昨日涨跌比": round(nums.get("yestRase", 0), 2)
            }
            sectors = []
            for sector_data in result.get("list", []):
                sector_info = {
                    "sector_code": sector_data.get("ZSCode", ""),
                    "sector_name": sector_data.get("ZSName", ""),
                    "stock_count": sector_data.get("num", 0), "stocks": []
                }
                for stock in sector_data.get("StockList", []):
                    if len(stock) >= 19:
                        seal_time_raw = stock[14] if stock[14] else ""
                        seal_time = ""
                        if seal_time_raw:
                            try:
                                hour = int(seal_time_raw)
                                minute_decimal = seal_time_raw - hour
                                minute = int(round(minute_decimal * 60))
                                if minute >= 60:
                                    hour += minute // 60
                                    minute = minute % 60
                                if 0 <= hour <= 23:
                                    seal_time = f"{hour:02d}:{minute:02d}:00"
                                else:
                                    seal_time = str(seal_time_raw)
                            except:
                                seal_time = str(seal_time_raw)
                        stock_info = {
                            "股票代码": stock[0], "股票名称": stock[1],
                            "涨停价": round(stock[4], 2) if stock[4] else 0, "成交额": 0,
                            "流通市值": stock[8] if stock[8] else 0,
                            "连板天数": stock[9], "连板次数": stock[10],
                            "概念标签": stock[11], "封单额": stock[12] if stock[12] else 0,
                            "主力资金": stock[13] if stock[13] else 0,
                            "首次封板时间": seal_time, "总市值": stock[15] if stock[15] else 0,
                            "涨停原因": stock[16] if stock[16] else "",
                            "主题": stock[17] if stock[17] else "",
                            "是否首板": stock[18] if len(stock) > 18 else 0,
                            "涨停时间": seal_time,
                        }
                        sector_info["stocks"].append(stock_info)
                sectors.append(sector_info)
            return {"summary": summary, "sectors": sectors}
        except Exception as e:
            print(f"请求板块数据失败 ({date}): {e}")
            return {"summary": {}, "sectors": []}

    def get_consecutive_limit_up(self, date=None, timeout=None):
        """获取指定日期的连板梯队情况"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        ladder_data = {}
        max_consecutive = 0
        max_stocks = []
        for pid_type in range(20, 1, -1):
            data = {
                "Order": "0", "a": "DailyLimitPerformance", "st": "2000",
                "c": "HisHomeDingPan", "PhoneOSNew": "1",
                "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
                "Index": "0", "PidType": str(pid_type), "apiv": "w42",
                "Type": "4", "Day": date
            }
            try:
                response = requests.post(self.base_url, data=data, headers=self.headers,
                                         verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
                response.raise_for_status()
                result = response.json()
                if result and result.get("errcode") == "0":
                    info = result.get("info", [])
                    if info and len(info) > 0 and len(info[0]) > 0:
                        stock_list = info[0]
                        stocks = []
                        for stock_data in stock_list:
                            if len(stock_data) >= 13:
                                stock_info = {
                                    "股票代码": stock_data[0], "股票名称": stock_data[1],
                                    "连板天数": stock_data[9] if len(stock_data) > 9 else pid_type,
                                    "题材": stock_data[5] if len(stock_data) > 5 else "",
                                    "概念": stock_data[12] if len(stock_data) > 12 else ""
                                }
                                stocks.append(stock_info)
                        if stocks:
                            ladder_data[pid_type] = stocks
                            if pid_type > max_consecutive:
                                max_consecutive = pid_type
                                max_stocks = stocks
            except:
                continue
        if max_consecutive == 0:
            return {"date": date, "max_consecutive": 0, "max_consecutive_stocks": "",
                    "max_consecutive_concepts": "", "ladder": {}}
        stock_names = []
        stock_concepts_list = []
        for stock in max_stocks:
            stock_names.append(stock["股票名称"])
            all_concepts = []
            if stock["题材"]:
                concepts = [c.strip() for c in stock["题材"].replace("/", "、").split("、") if c.strip()]
                all_concepts.extend(concepts)
            if stock["概念"]:
                concepts = [c.strip() for c in stock["概念"].replace("/", "、").split("、") if c.strip()]
                all_concepts.extend(concepts)
            unique_concepts = []
            seen = set()
            for c in all_concepts:
                if c not in seen:
                    unique_concepts.append(c)
                    seen.add(c)
            stock_concept = "、".join(unique_concepts) if unique_concepts else ""
            stock_concepts_list.append(stock_concept)
        max_consecutive_stocks = "/".join(stock_names)
        max_consecutive_concepts = "/".join([c for c in stock_concepts_list if c])
        return {
            "date": date, "max_consecutive": max_consecutive,
            "max_consecutive_stocks": max_consecutive_stocks,
            "max_consecutive_concepts": max_consecutive_concepts,
            "ladder": ladder_data
        }

    def get_sector_limit_up_ladder(self, date=None, timeout=None):
        """获取板块连板梯队"""
        is_realtime = date is None
        if is_realtime:
            url, headers = self.sector_base_url, self.sector_headers
            data_params = {
                "a": "GetYTFP_BKHX", "c": "FuPanLa", "PhoneOSNew": "1",
                "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2", "apiv": "w42"
            }
            display_date = datetime.now().strftime("%Y-%m-%d")
        else:
            url, headers = self.base_url, self.headers
            data_params = {
                "a": "GetYTFP_BKHX", "c": "FuPanLa", "PhoneOSNew": "1",
                "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
                "Date": date, "apiv": "w42"
            }
            display_date = date
        try:
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {"date": display_date, "is_realtime": is_realtime, "sectors": []}
            sectors = []
            for sector_data in result.get("List", []):
                sector_name = sector_data.get("ZSName", "")
                sector_code = sector_data.get("ZSCode", "")
                stocks, broken_stocks = [], []
                for td_group in sector_data.get("TD", []):
                    td_type = td_group.get("TDType", "1")
                    for stock_data in td_group.get("Stock", []):
                        stock_code = stock_data.get("StockID", "")
                        stock_name = stock_data.get("StockName", "")
                        tips = stock_data.get("Tips", "")
                        if td_type == "0":
                            consecutive_days = 0
                            if tips:
                                import re
                                match = re.search(r'(\d+)天(\d+)板', tips)
                                if match:
                                    consecutive_days = int(match.group(2))
                            broken_stocks.append({"stock_code": stock_code, "stock_name": stock_name,
                                                  "consecutive_days": consecutive_days, "tips": tips, "is_broken": True})
                        elif td_type == "9":
                            stocks.append({"stock_code": stock_code, "stock_name": stock_name,
                                           "consecutive_days": 0, "tips": tips, "is_height_mark": True})
                        else:
                            try:
                                cd = int(td_type)
                            except:
                                cd = 1
                            stocks.append({"stock_code": stock_code, "stock_name": stock_name,
                                           "consecutive_days": cd, "tips": tips})
                if stocks or broken_stocks:
                    sectors.append({"sector_code": sector_code, "sector_name": sector_name,
                                    "limit_up_count": int(sector_data.get("Count", len(stocks))),
                                    "stocks": stocks, "broken_stocks": broken_stocks})
            return {"date": result.get("Date", display_date), "is_realtime": is_realtime, "sectors": sectors}
        except Exception as e:
            print(f"请求板块连板梯队失败 ({display_date}): {e}")
            return {"date": display_date, "is_realtime": is_realtime, "sectors": []}

    def get_market_limit_up_ladder(self, date=None, timeout=None):
        """获取全市场连板梯队"""
        is_realtime = date is None
        if is_realtime:
            url, headers = self.sector_base_url, self.sector_headers
            data_params = {
                "a": "GetYTFP_SCTD", "c": "FuPanLa", "PhoneOSNew": "1",
                "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2", "apiv": "w42"
            }
            display_date = datetime.now().strftime("%Y-%m-%d")
        else:
            url, headers = self.base_url, self.headers
            data_params = {
                "a": "GetYTFP_SCTD", "c": "FuPanLa", "PhoneOSNew": "1",
                "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
                "Date": date, "apiv": "w42"
            }
            display_date = date
        try:
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {"date": display_date, "is_realtime": is_realtime, "ladder": {},
                        "broken_stocks": [], "height_marks": [], "statistics": {
                            "total_limit_up": 0, "max_consecutive": 0, "ladder_distribution": {}}}
            ladder, broken_stocks, height_marks = {}, [], []
            for group in result.get("List", []):
                tip = group.get("Tip", "1")
                for stock_data in group.get("Stocks", []):
                    stock_code = stock_data.get("StockID", "")
                    stock_name = stock_data.get("Name", "")
                    tips = stock_data.get("Tips", "")
                    if tip == "0":
                        consecutive_days = 0
                        if tips:
                            import re
                            match = re.search(r'(\d+)天(\d+)板', tips)
                            if match:
                                consecutive_days = int(match.group(2))
                        broken_stocks.append({"stock_code": stock_code, "stock_name": stock_name,
                                              "consecutive_days": consecutive_days, "tips": tips, "is_broken": True})
                    elif tip == "9":
                        height_marks.append({"stock_code": stock_code, "stock_name": stock_name,
                                             "consecutive_days": 0, "tips": tips, "is_height_mark": True})
                    else:
                        try:
                            cd = int(tip)
                        except:
                            cd = 1
                        ladder.setdefault(cd, []).append(
                            {"stock_code": stock_code, "stock_name": stock_name,
                             "consecutive_days": cd, "tips": tips})
            total = sum(len(v) for v in ladder.values())
            max_consecutive = max(ladder.keys()) if ladder else 0
            return {"date": result.get("Date", display_date), "is_realtime": is_realtime,
                    "ladder": ladder, "broken_stocks": broken_stocks, "height_marks": height_marks,
                    "statistics": {"total_limit_up": total, "max_consecutive": max_consecutive,
                                   "ladder_distribution": {k: len(v) for k, v in ladder.items()}}}
        except Exception as e:
            print(f"请求全市场连板梯队失败 ({display_date}): {e}")
            return {"date": display_date, "is_realtime": is_realtime, "ladder": {},
                    "broken_stocks": [], "height_marks": [], "statistics": {
                        "total_limit_up": 0, "max_consecutive": 0, "ladder_distribution": {}}}

    def get_historical_broken_limit_up(self, date, timeout=None):
        """获取历史炸板股数据"""
        try:
            data = {
                "Order": "1", "a": "HisDaBanList", "st": "30", "c": "HisHomeDingPan",
                "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
                "Index": "0", "Is_st": "1", "PidType": "2", "apiv": "w42", "Type": "4",
                "FilterMotherboard": "0", "Filter": "0", "FilterTIB": "0", "Day": date,
                "FilterGem": "0"
            }
            response = requests.post(self.base_url, data=data, headers=self.headers,
                                     verify=False, timeout=timeout or 60)
            if response.status_code != 200:
                return []
            result = response.json()
            broken_stocks = []
            for stock_data in result.get("list", []):
                if len(stock_data) < 16:
                    continue
                broken_stocks.append({
                    "stock_code": stock_data[0], "stock_name": stock_data[1],
                    "change_pct": float(stock_data[4]) if stock_data[4] else 0,
                    "limit_up_time": int(stock_data[6]) if stock_data[6] else 0,
                    "open_time": int(stock_data[7]) if stock_data[7] else 0,
                    "yesterday_consecutive_text": stock_data[9] if stock_data[9] else "",
                    "yesterday_consecutive": int(stock_data[10]) if stock_data[10] else 0,
                    "sector": stock_data[11] if stock_data[11] else "",
                    "main_capital_net": float(stock_data[12]) if stock_data[12] else 0,
                    "turnover_amount": float(stock_data[13]) if stock_data[13] else 0,
                    "turnover_rate": float(stock_data[14]) if stock_data[14] else 0,
                    "actual_circulation": stock_data[15] if stock_data[15] else ""
                })
            return broken_stocks
        except Exception as e:
            print(f"获取历史炸板股失败 ({date}): {e}")
            return []

    def get_sector_capital_data(self, sector_code, date=None, timeout=None):
        """获取板块资金成交额数据"""
        if date:
            url, headers = self.base_url, self.headers
        else:
            url, headers = self.sector_base_url, self.sector_headers
        data_params = {
            "a": "GetPanKou", "c": "ZhiShuL2Data", "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
            "apiv": "w42", "StockID": sector_code, "Day": date if date else ""
        }
        try:
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            pankou = result.get("pankou", [])
            if len(pankou) < 11:
                return {}
            capital_data = {
                "sector_code": result.get("code", sector_code),
                "date": date if date else datetime.now().strftime("%Y-%m-%d"),
                "turnover": float(pankou[0]) if pankou[0] else 0,
                "change_pct": float(pankou[1]) if pankou[1] else 0,
                "market_cap": float(pankou[2]) if pankou[2] else 0,
                "main_net_inflow": float(pankou[3]) if pankou[3] else 0,
                "main_sell": float(pankou[4]) if pankou[4] else 0,
                "net_amount": float(pankou[5]) if pankou[5] else 0,
                "up_count": int(pankou[6]) if pankou[6] else 0,
                "down_count": int(pankou[7]) if pankou[7] else 0,
                "flat_count": int(pankou[8]) if pankou[8] else 0,
                "circulating_market_cap": float(pankou[9]) if pankou[9] else 0,
                "total_market_cap": float(pankou[10]) if pankou[10] else 0,
                "turnover_rate": float(pankou[11]) if len(pankou) > 11 and pankou[11] else 0,
            }
            capital_data["main_net_inflow_pct"] = (capital_data["main_net_inflow"] / capital_data["turnover"] * 100) if capital_data["turnover"] > 0 else 0
            return capital_data
        except Exception as e:
            print(f"请求板块资金数据失败 ({sector_code}): {e}")
            return {}

    def get_sector_strength_ndays(self, end_date, num_days=7, timeout=None):
        """获取N日板块强度排名数据"""
        end = datetime.strptime(end_date, "%Y-%m-%d")
        dates, current = [], end
        for i in range(num_days * 2):
            dates.append(current.strftime("%Y-%m-%d"))
            current -= timedelta(days=1)
        all_data, trading_days_count = [], 0
        for date in dates:
            if trading_days_count >= num_days:
                break
            try:
                sector_data = self.get_sector_ranking(date, timeout=timeout)
                if not sector_data or not sector_data.get("sectors"):
                    continue
                trading_days_count += 1
                for sector in sector_data["sectors"]:
                    stock_codes = [s.get("股票代码", "") for s in sector.get("stocks", [])]
                    row = {"日期": date, "板块代码": sector.get("sector_code", ""),
                           "板块名称": sector.get("sector_name", ""),
                           "涨停数": sector.get("stock_count", 0),
                           "涨停股票": ",".join(stock_codes)}
                    all_data.append(row)
            except:
                continue
        return pd.DataFrame(all_data) if all_data else pd.DataFrame()

    def get_realtime_market_mood(self, timeout=None):
        """获取实时市场情绪数据"""
        data_params = {
            "a": "MoodNumCount", "c": "MarketMood", "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2", "apiv": "w42"
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            ld = result.get("list", {})
            return {
                "上涨家数": int(ld.get("SZJS", 0)), "下跌家数": int(ld.get("XDJS", 0)),
                "涨停家数": int(ld.get("ZTJS", 0)), "跌停家数": int(ld.get("DTJS", 0)),
                "全市场流通量": int(ld.get("qscln", 0)), "前日流通量": int(ld.get("q_zrcs", 0)),
                "涨跌比": float(ld.get("bl", 0)), "市场颜色": int(ld.get("color", 0))
            }
        except Exception as e:
            print(f"请求实时市场情绪失败: {e}")
            return {}

    def get_realtime_actual_limit_up_down(self, timeout=None):
        """获取实时实际涨跌停数据"""
        data_params = {
            "a": "MarketStockZDNum", "c": "HomeDingPan", "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2", "apiv": "w42"
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            return {
                "actual_limit_up": int(result.get("actual_limit_up", 0)),
                "actual_limit_down": int(result.get("actual_limit_down", 0)),
                "limit_up": int(result.get("limit_up", 0)),
                "limit_down": int(result.get("limit_down", 0)),
            }
        except Exception as e:
            print(f"请求实时实际涨跌停数据失败: {e}")
            return {}

    def get_realtime_board_stocks(self, board_type=1, timeout=None):
        """获取实时指定连板的股票列表"""
        data_params = {
            "Order": "0", "a": "DailyLimitPerformance", "st": "2000", "c": "HomeDingPan",
            "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
            "Index": "0", "PidType": str(board_type), "apiv": "w42", "Type": "4"
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return []
            info = result.get("info", [])
            if not info or len(info) < 1:
                return []
            stock_list = info[0] if isinstance(info[0], list) else []
            stocks = []
            for stock_data in stock_list:
                if not isinstance(stock_data, list) or len(stock_data) < 23:
                    continue
                stocks.append({
                    "stock_code": stock_data[0], "stock_name": stock_data[1],
                    "board_type": board_type, "timestamp": stock_data[4],
                    "limit_up_reason": stock_data[5], "turnover": stock_data[6],
                    "circulating_market_cap": stock_data[7], "main_buy": stock_data[8],
                    "main_sell": stock_data[9], "main_net_inflow": stock_data[10],
                    "seal_amount": stock_data[11], "concepts": stock_data[12],
                    "total_market_cap": stock_data[13], "amplitude": stock_data[14],
                    "consecutive_days": stock_data[15],
                    "change_pct": stock_data[17] if len(stock_data) > 17 else 0,
                    "sector_code": stock_data[19] if len(stock_data) > 19 else "",
                    "sector_limit_up_count": stock_data[20] if len(stock_data) > 20 else 0,
                    "limit_up_price": stock_data[21] if len(stock_data) > 21 else 0,
                    "limit_up_pct": stock_data[22] if len(stock_data) > 22 else 0,
                })
            return stocks
        except Exception as e:
            print(f"请求实时{board_type}板股票失败: {e}")
            return []

    def get_realtime_all_boards_stocks(self, timeout=None):
        """获取实时所有连板的股票列表"""
        board_names = {1: "first_board", 2: "second_board", 3: "third_board",
                       4: "fourth_board", 5: "fifth_board_plus"}
        all_boards, total_stocks = {}, 0
        for board_type, board_name in board_names.items():
            stocks = self.get_realtime_board_stocks(board_type, timeout)
            all_boards[board_name] = stocks
            total_stocks += len(stocks)
        stats = {"total_stocks": total_stocks}
        for name in board_names.values():
            stats[f"{name}_count"] = len(all_boards[name])
        stats["consecutive_rate"] = ((total_stocks - len(all_boards["first_board"])) / total_stocks * 100) if total_stocks > 0 else 0
        all_boards["statistics"] = stats
        return all_boards

    def get_board_stocks_count_and_list(self, board_type, timeout=None):
        """获取指定连板的个股数量和列表"""
        stocks = self.get_realtime_board_stocks(board_type, timeout)
        return len(stocks), stocks

    def get_realtime_index_trend(self, stock_id="801900", time="15:00", timeout=None):
        """获取实时指数趋势数据"""
        data_params = {
            "a": "GetTrendIncremental", "apiv": "w42", "c": "ZhiShuL2Data",
            "StockID": stock_id, "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
            "Time": time, "Day": ""
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            trend_data = result.get("trend", [])
            preclose_px = result.get("preclose_px", 0)
            if trend_data and len(trend_data) > 0:
                close_price = trend_data[0][1]
                change_pct = (close_price - preclose_px) / preclose_px * 100 if preclose_px != 0 else 0
            else:
                change_pct = 0
            return {"stock_id": stock_id, "date": result.get("day", ""), "time": time,
                    "value": change_pct, "change_pct": change_pct,
                    "intraday_data": result.get("trend", []), "raw_data": result}
        except Exception as e:
            print(f"请求实时指数趋势失败: {e}")
            return {}

    def get_realtime_index_list(self, stock_ids=None, timeout=None):
        """获取实时指数列表数据"""
        if stock_ids is None:
            stock_ids = ["SH000001", "SZ399001", "SZ399006", "SH000688"]
        stock_id_list = ",".join(stock_ids)
        data_params = {
            "a": "RefreshStockList", "c": "UserSelectStock", "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2", "Token": "",
            "apiv": "w42", "StockIDList": stock_id_list, "UserID": ""
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            indexes = []
            for stock in result.get("StockList", []):
                indexes.append({
                    "stock_id": stock.get("StockID", ""),
                    "name": stock.get("prod_name", ""),
                    "value": float(stock.get("last_px", 0)),
                    "change_pct": float(stock.get("increase_rate", "0").replace("%", "")),
                    "change_amount": float(stock.get("increase_amount", 0)),
                    "turnover": int(stock.get("turnover", 0))
                })
            return {"indexes": indexes, "raw_data": result}
        except Exception as e:
            print(f"请求实时指数列表失败: {e}")
            return {}

    def get_realtime_sharp_withdrawal(self, timeout=None):
        """获取实时大幅回撤股票数据"""
        data_params = {
            "Order": "0", "a": "SharpWithdrawalList", "st": "20", "c": "HomeDingPan",
            "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
            "Index": "0", "apiv": "w42", "Type": "5"
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            info = result.get("info", [])
            stocks = []
            for stock_data in info:
                if len(stock_data) >= 7:
                    stocks.append({
                        "stock_code": stock_data[0], "stock_name": stock_data[1],
                        "board_type": stock_data[2], "tag": stock_data[3],
                        "latest_price": float(stock_data[4]),
                        "change_pct": float(stock_data[5]),
                        "pullback_pct": float(stock_data[6])
                    })
            return {"date": result.get("date", ""), "count": result.get("num", 0),
                    "stocks": stocks, "raw_data": result}
        except Exception as e:
            print(f"请求实时大幅回撤数据失败: {e}")
            return {}

    def get_realtime_rise_fall_analysis(self, timeout=None):
        """获取实时涨跌分析数据"""
        data_params = {
            "a": "RiseFallAnalysis", "st": "250", "c": "HisHomeDingPan",
            "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()),
            "VerSion": "5.21.0.2", "Index": "0", "apiv": "w42"
        }
        try:
            response = requests.post(self.base_url, data=data_params, headers=self.headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return {}
            info = result.get("info", [])
            if not info or len(info) < 1 or len(info[0]) < 7:
                return {}
            latest = info[0]
            zt_data = self.get_realtime_index_trend(stock_id="801900", time="15:00", timeout=timeout)
            yesterday_limit_up_performance = zt_data.get("change_pct", 0.0) if zt_data else 0.0
            pb_data = self.get_realtime_index_trend(stock_id="801903", time="15:00", timeout=timeout)
            yesterday_broken_performance = pb_data.get("change_pct", 0.0) if pb_data else 0.0
            return {
                "date": latest[6], "limit_up_count": int(latest[0]),
                "limit_down_count": int(latest[1]), "broken_limit_up_count": int(latest[2]),
                "blown_limit_up_count": int(latest[3]), "blown_limit_up_rate": float(latest[4]),
                "yesterday_limit_up_performance": yesterday_limit_up_performance,
                "yesterday_broken_performance": yesterday_broken_performance,
                "raw_data": info
            }
        except Exception as e:
            print(f"请求实时涨跌分析失败: {e}")
            return {}

    def get_sector_intraday(self, sector_code, date=None, timeout=300):
        """获取板块分时数据"""
        is_realtime = date is None
        if is_realtime:
            url, headers = self.sector_base_url, self.sector_headers
            display_date = datetime.now().strftime("%Y-%m-%d")
        else:
            url, headers = self.base_url, self.headers
            display_date = date
        data_params = {
            "a": "GetTrendIncremental", "c": "ZhiShuL2Data", "PhoneOSNew": "1",
            "DeviceID": "e78ba169-6c03-3faf-8e5e-a72f8411a8eb",
            "VerSion": "5.21.0.2", "apiv": "w42", "StockID": sector_code,
            "Day": date if date else ""
        }
        try:
            time.sleep(0.5)
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            if not response.text.strip():
                return {}
            try:
                result = response.json()
            except:
                return {}
            if not result or result.get("errcode") != "0":
                return {}
            trend_data = result.get("trend", [])
            if not trend_data:
                return {}
            records = []
            for item in trend_data:
                if len(item) >= 5:
                    records.append({"time": item[0], "price": float(item[1]),
                                    "volume": int(item[2]), "turnover": float(item[3]),
                                    "trend": int(item[4])})
            df = pd.DataFrame(records)
            open_price = df['price'].iloc[0] if len(df) > 0 else 0
            close_price = df['price'].iloc[-1] if len(df) > 0 else 0
            high_price = df['price'].max() if len(df) > 0 else 0
            low_price = df['price'].min() if len(df) > 0 else 0
            preclose = float(result.get("preclose", open_price))
            return {"sector_code": sector_code, "date": result.get("date", display_date),
                    "open": open_price, "close": close_price, "high": high_price,
                    "low": low_price, "preclose": preclose, "data": df}
        except Exception as e:
            print(f"请求板块分时数据失败 ({sector_code}): {e}")
            return {}

    def get_sector_volume_turnover(self, sector_code, date=None, timeout=300):
        """获取板块分时成交量和成交额数据"""
        is_realtime = date is None
        if is_realtime:
            url, headers = self.sector_base_url, self.sector_headers
            display_date = datetime.now().strftime("%Y-%m-%d")
        else:
            url, headers = self.base_url, self.headers
            display_date = date
        data_params = {
            "a": "GetVolTurIncremental", "c": "ZhiShuL2Data", "PhoneOSNew": "1",
            "DeviceID": "e78ba169-6c03-3faf-8e5e-a72f8411a8eb",
            "VerSion": "5.21.0.2", "apiv": "w42", "StockID": sector_code,
            "Day": date if date else ""
        }
        try:
            time.sleep(0.5)
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            if not response.text.strip():
                return {}
            try:
                result = response.json()
            except:
                return {}
            if not result or result.get("errcode") != "0":
                return {}
            vt_data = result.get("volumeturnover", [])
            if not vt_data:
                return {}
            records = []
            for item in vt_data:
                if len(item) >= 3:
                    records.append({"time": item[0], "volume": int(item[1]) if item[1] else 0,
                                    "turnover": float(item[2]) if item[2] else 0,
                                    "unknown": int(item[3]) if len(item) > 3 and item[3] else 0})
            df = pd.DataFrame(records)
            return {"sector_code": result.get("code", sector_code),
                    "date": result.get("day", display_date), "data": df}
        except Exception as e:
            print(f"请求板块成交量/成交额数据失败 ({sector_code}): {e}")
            return {}

    def get_stock_intraday(self, stock_code, date=None, timeout=300):
        """获取个股分时数据"""
        is_realtime = date is None
        display_date = datetime.now().strftime("%Y-%m-%d") if is_realtime else date
        is_index = stock_code.startswith('SH') or (stock_code.startswith('SZ') and '399' in stock_code)

        if is_index:
            if is_realtime:
                data_params = {
                    "a": "GetZstrend", "apiv": "w42", "c": "StockL2Data",
                    "StockID": stock_code, "PhoneOSNew": "1", "UserID": "0",
                    "DeviceID": "e78ba169-6c03-3faf-8e5e-a72f8411a8eb",
                    "VerSion": "5.21.0.2", "Token": "0"
                }
                url, headers = self.sector_base_url, self.sector_headers
            else:
                url, headers = "https://apphis.longhuvip.com/w1/api/index.php", self.headers
                data_params = {
                    "a": "GetStockTrend", "c": "StockL2History", "PhoneOSNew": "1",
                    "DeviceID": "e78ba169-6c03-3faf-8e5e-a72f8411a8eb",
                    "VerSion": "5.21.0.2", "Token": "0", "apiv": "w42",
                    "StockID": stock_code, "UserID": "0",
                    "Day": date.replace('-', '')
                }
        else:
            url, headers = "https://apphis.longhuvip.com/w1/api/index.php", self.headers
            data_params = {
                "a": "GetStockTrend", "apiv": "w42", "c": "StockL2History",
                "StockID": stock_code, "PhoneOSNew": "1", "UserID": "0",
                "DeviceID": "e78ba169-6c03-3faf-8e5e-a72f8411a8eb",
                "VerSion": "5.21.0.2", "Token": "0"
            }
            if date:
                data_params["Day"] = date.replace('-', '')

        try:
            time.sleep(0.5)
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            if not response.text.strip():
                return {}
            try:
                result = response.json()
            except:
                return {}
            if not result or result.get("errcode") != "0":
                return {}
            trend_data = result.get("trend", [])
            if not trend_data:
                return {}
            records = []
            for item in trend_data:
                if len(item) >= 6:
                    records.append({
                        "time": item[0], "price": float(item[1]),
                        "avg_price": float(item[2]), "volume": int(item[3]),
                        "turnover": float(item[4]), "main_net_inflow": float(item[5]),
                        "flag": int(item[6]) if len(item) > 6 else 0
                    })
            df = pd.DataFrame(records)
            total_main_inflow = df[df['main_net_inflow'] > 0]['main_net_inflow'].sum() if len(df) > 0 else 0
            total_main_outflow = df[df['main_net_inflow'] < 0]['main_net_inflow'].sum() if len(df) > 0 else 0
            return {"stock_code": stock_code, "date": result.get("date", display_date),
                    "total_main_inflow": total_main_inflow, "total_main_outflow": total_main_outflow,
                    "data": df}
        except Exception as e:
            print(f"请求个股分时数据失败 ({stock_code}): {e}")
            return {}

    def get_abnormal_stocks(self, timeout=None):
        """获取异动个股数据（实时）"""
        data_params = {
            "a": "GetRealTimeAbnormalStock", "c": "AbnormalMonitor",
            "PhoneOSNew": "1", "DeviceID": str(uuid.uuid4()),
            "VerSion": "5.21.0.2", "apiv": "w42", "Index": "0", "st": "100"
        }
        try:
            response = requests.post(self.sector_base_url, data=data_params, headers=self.sector_headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return pd.DataFrame()
            stock_list = result.get("list", [])
            if not stock_list:
                return pd.DataFrame()
            records = []
            for item in stock_list:
                if len(item) >= 6:
                    records.append({"时间": item[0], "股票代码": item[1], "股票名称": item[2],
                                    "异动类型": item[3], "涨跌幅": float(item[4]), "最新价": float(item[5])})
            return pd.DataFrame(records)
        except Exception as e:
            print(f"请求异动个股数据失败: {e}")
            return pd.DataFrame()

    def get_sector_strength(self, sector_code, date=None, timeout=None):
        """获取板块强度数据"""
        if date:
            url, headers = self.base_url, self.headers
        else:
            url, headers = self.sector_base_url, self.sector_headers
        data_params = {
            "a": "GetStockMsZJZX", "c": "ZhiShuL2Data", "PhoneOSNew": "1",
            "DeviceID": str(uuid.uuid4()), "VerSion": "5.21.0.2",
            "apiv": "w42", "StockID": sector_code, "Day": date if date else ""
        }
        try:
            response = requests.post(url, data=data_params, headers=headers,
                                     verify=False, proxies={'http': None, 'https': None}, timeout=timeout)
            response.raise_for_status()
            result = response.json()
            if not result or result.get("errcode") != "0":
                return 0.0
            return float(result.get("ms", 0))
        except Exception as e:
            print(f"请求板块强度数据失败 ({sector_code}): {e}")
            return 0.0

    def get_multiple_sectors_strength(self, sector_codes, date=None, timeout=None):
        """批量获取多个板块的强度数据"""
        results = {}
        for sector_code in sector_codes:
            results[sector_code] = self.get_sector_strength(sector_code, date, timeout)
        return results

    def get_sector_strength_history(self, sector_code, start_date, end_date, timeout=None):
        """获取板块强度历史数据"""
        date_list = get_trading_dates(start_date, end_date)
        strengths = {}
        for date in date_list:
            strengths[date] = self.get_sector_strength(sector_code, date, timeout)
        series = pd.Series(strengths)
        series.index.name = "日期"
        series.name = f"{sector_code}_强度"
        return series

    def get_sector_strength_dataframe(self, sector_code, start_date, end_date, timeout=None):
        """获取板块强度历史DataFrame"""
        series = self.get_sector_strength_history(sector_code, start_date, end_date, timeout)
        df = series.reset_index()
        df.columns = ["日期", "强度"]
        return df
