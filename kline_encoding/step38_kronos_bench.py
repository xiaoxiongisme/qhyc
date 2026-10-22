# -*- coding: utf-8 -*-
"""Step 38: Kronos 基础模型基准（零样本，CPU）
任务与 HMM/链完全同口径：下一根振幅五档，holdout(2026)，log-loss + top-1。
方法：每个评估点取前 512 根上下文，复制 auto_regressive_inference 的采样循环
（保留每样本 OHLC，不做均值），sample_count=20 构建经验分布。
"""
import sys, os, time
import numpy as np
import pandas as pd

KRONOS_DIR = 'E:/抖音分析/2026-10-16-19-50-44/kline_encoding/kronos_work/Kronos'
MODEL_DIR = 'E:/抖音分析/2026-10-16-19-50-44/kline_encoding/kronos_work/Kronos_models'
sys.path.insert(0, KRONOS_DIR)
import torch
from model import Kronos, KronosTokenizer, KronosPredictor
from model.kronos import calc_time_stamps, sample_from_logits

torch.set_num_threads(6)
N_POINTS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
SYMS = sys.argv[2].split(',') if len(sys.argv) > 2 else ['MA', 'EG']
SAMPLE_COUNT = 20
CTX = 512
PARTS = ['2020', '2021_2025', '2026']

tokenizer = KronosTokenizer.from_pretrained(os.path.join(MODEL_DIR, 'Kronos-Tokenizer-base'))
model = Kronos.from_pretrained(os.path.join(MODEL_DIR, 'Kronos-base'))
predictor = KronosPredictor(model, tokenizer, max_context=CTX)
model.eval()
print('model loaded', flush=True)


def sample_forecasts(x, x_stamp, y_stamp, pred_len=1, T=1.0, top_k=0, top_p=0.95):
    """复制 auto_regressive_inference，但保留每样本预测（不求均值）。
    x: (1, seq, 6) 已归一化；返回 (sample_count, pred_len, 6)"""
    with torch.no_grad():
        x = torch.clip(x, -predictor.clip, predictor.clip)
        x = x.unsqueeze(1).repeat(1, SAMPLE_COUNT, 1, 1).reshape(-1, x.size(1), x.size(2))
        x_stamp = x_stamp.unsqueeze(1).repeat(1, SAMPLE_COUNT, 1, 1).reshape(-1, x_stamp.size(1), x_stamp.size(2))
        y_stamp = y_stamp.unsqueeze(1).repeat(1, SAMPLE_COUNT, 1, 1).reshape(-1, y_stamp.size(1), y_stamp.size(2))
        x_token = tokenizer.encode(x, half=True)
        initial_seq_len = x.size(1)
        batch_size = x_token[0].size(0)
        total_seq_len = initial_seq_len + pred_len
        full_stamp = torch.cat([x_stamp, y_stamp], dim=1)
        generated_pre = x_token[0].new_empty(batch_size, pred_len)
        generated_post = x_token[1].new_empty(batch_size, pred_len)
        pre_buffer = x_token[0].new_zeros(batch_size, CTX)
        post_buffer = x_token[1].new_zeros(batch_size, CTX)
        buffer_len = min(initial_seq_len, CTX)
        if buffer_len > 0:
            start_idx = max(0, initial_seq_len - CTX)
            pre_buffer[:, :buffer_len] = x_token[0][:, start_idx:start_idx + buffer_len]
            post_buffer[:, :buffer_len] = x_token[1][:, start_idx:start_idx + buffer_len]
        for i in range(pred_len):
            current_seq_len = initial_seq_len + i
            window_len = min(current_seq_len, CTX)
            input_tokens = [pre_buffer[:, :window_len], post_buffer[:, :window_len]]
            context_end = current_seq_len
            context_start = max(0, context_end - CTX)
            current_stamp = full_stamp[:, context_start:context_end, :].contiguous()
            s1_logits, context = model.decode_s1(input_tokens[0], input_tokens[1], current_stamp)
            s1_logits = s1_logits[:, -1, :]
            sample_pre = sample_from_logits(s1_logits, temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)
            s2_logits = model.decode_s2(context, sample_pre)
            s2_logits = s2_logits[:, -1, :]
            sample_post = sample_from_logits(s2_logits, temperature=T, top_k=top_k, top_p=top_p, sample_logits=True)
            generated_pre[:, i] = sample_pre.squeeze(-1)
            generated_post[:, i] = sample_post.squeeze(-1)
            if current_seq_len < CTX:
                pre_buffer[:, current_seq_len] = sample_pre.squeeze(-1)
                post_buffer[:, current_seq_len] = sample_post.squeeze(-1)
            else:
                pre_buffer.copy_(torch.roll(pre_buffer, shifts=-1, dims=1))
                post_buffer.copy_(torch.roll(post_buffer, shifts=-1, dims=1))
                pre_buffer[:, -1] = sample_pre.squeeze(-1)
                post_buffer[:, -1] = sample_post.squeeze(-1)
        full_pre = torch.cat([x_token[0], generated_pre], dim=1)
        full_post = torch.cat([x_token[1], generated_post], dim=1)
        context_start = max(0, total_seq_len - CTX)
        z = tokenizer.decode([full_pre[:, context_start:total_seq_len].contiguous(),
                              full_post[:, context_start:total_seq_len].contiguous()], half=True)
        return z.cpu().numpy()      # (sample_count, pred_len, 6)


def load_sym(sym):
    ds = []
    for p in PARTS:
        fn = f'{sym}_{p}_1m_raw.csv'
        if os.path.getsize(fn) > 300:
            ds.append(pd.read_csv(fn, parse_dates=['datetime']))
    d = pd.concat(ds, ignore_index=True).sort_values('datetime').reset_index(drop=True)
    d['amp'] = (d.high - d.low) / d.open * 100.0
    d['logret'] = np.log(d.close / d.open)
    gap = d.datetime.diff().dt.total_seconds().values
    d['adj'] = np.concatenate([[False], gap[1:] == 60.0])
    return d


out = {}
for sym in SYMS:
    d = load_sym(sym)
    n = len(d)
    yr = d.year if 'year' in d.columns else pd.to_datetime(d.datetime).dt.year
    amp = d.amp.values
    mtr = (yr <= 2025).values
    qs = np.quantile(amp[mtr], [0.2, 0.4, 0.6, 0.8])
    cls = np.clip(np.digitize(amp, qs), 0, 4)
    # holdout 相邻对评估点（bar t 预测 t+1），要求 t 前有 >=CTX+10 根上下文
    cand = np.where((yr.values[1:] == 2026) & d.adj.values[1:] & (np.arange(1, n) >= CTX + 10))[0] + 1
    cand = cand[np.arange(0, len(cand), max(1, len(cand) // N_POINTS))][:N_POINTS]
    o, h, l, c, v = d.open.values, d.high.values, d.low.values, d.close.values, d.volume.values
    amt = v * (o + h + l + c) / 4.0
    nlls, tops, preds, t0 = [], [], [], time.time()
    for ii, t in enumerate(cand):
        s0 = t - CTX
        feats = np.stack([o[s0:t], h[s0:t], l[s0:t], c[s0:t], v[s0:t], amt[s0:t]], axis=1).astype(np.float32)
        x_mean, x_std = feats.mean(0), feats.std(0) + 1e-5
        xn = np.clip((feats - x_mean) / x_std, -5, 5)[None, :, :]
        x_ts = pd.Series(pd.to_datetime(d.datetime.values[s0:t]))
        y_ts = pd.Series(pd.to_datetime(d.datetime.values[t:t + 1]))
        xs = calc_time_stamps(x_ts).values.astype(np.float32)[None, :, :]
        ys = calc_time_stamps(y_ts).values.astype(np.float32)[None, :, :]
        z = sample_forecasts(torch.from_numpy(xn), torch.from_numpy(xs), torch.from_numpy(ys))
        pred = z[:, 0, :] * (x_std + 1e-5) + x_mean        # (S, 6) 还原
        amp_pred = (pred[:, 1] - pred[:, 2]) / np.maximum(pred[:, 0], 1e-8) * 100.0
        cp = np.clip(np.digitize(amp_pred, qs), 0, 4)
        hist = np.bincount(cp, minlength=5).astype(float)
        prob = (hist + 0.5) / (hist.sum() + 2.5)
        tgt = cls[t]
        nlls.append(-np.log2(prob[tgt]))
        tops.append(1.0 if prob.argmax() == tgt else 0.0)
        preds.append(int(prob.argmax()))
        if (ii + 1) % 20 == 0:
            print(f'  {sym} {ii+1}/{len(cand)} nll={np.mean(nlls):.3f} top1={np.mean(tops):.3f} '
                  f'({(time.time()-t0)/(ii+1):.1f}s/点)', flush=True)
    out[sym] = {'n': len(cand), 'nll': float(np.mean(nlls)), 'top1': float(np.mean(tops)),
                'nll_se': float(np.std(nlls) / np.sqrt(len(cand))),
                'pred_class_hist': np.bincount(np.array(preds), minlength=5).tolist()}
    print(f'{sym}: Kronos nll {out[sym]["nll"]:.3f}±{out[sym]["nll_se"]:.3f} top1 {out[sym]["top1"]:.3f} (n={len(cand)})', flush=True)

import json
# 合并写入：并行跑多个 sym 时避免后完成者覆盖先完成者
try:
    _old = json.load(open('results_kronos.json', encoding='utf-8'))
    if isinstance(_old, dict):
        _old.update(out)
        out = _old
except Exception:
    pass
json.dump(out, open('results_kronos.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('saved results_kronos.json')
