"""Seed-VC programatik sarmalayici — voice.stuqio.com hibrit klonlama boru hatti.

metin -> edge-tts (kaynak, mukemmel TR prozodisi) -> Seed-VC timbre transferi -> WAV.
inference.py'nin CLI'siz, tekrar-kullanilabilir versiyonu. Model bir kez yuklenir.
"""
import os
import sys
import warnings

warnings.simplefilter('ignore')


class SeedVCInfer:
    def __init__(self, seedvc_dir, device="cpu"):
        self.dir = os.path.abspath(seedvc_dir)
        self.device = device
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        if self.dir not in sys.path:
            sys.path.insert(0, self.dir)
        os.chdir(self.dir)  # hf_utils goreli checkpoint dizinleri kullanir
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "seedvc_inference", os.path.join(self.dir, "inference.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._inf = mod

        class _A:
            f0_condition = False
            checkpoint = None
            config = None
            fp16 = False
        (self.model, self.semantic_fn, self.f0_fn, self.vocoder_fn,
         self.campplus_model, self.mel_fn, self.mel_fn_args) = mod.load_models(_A())
        self.sr = self.mel_fn_args['sampling_rate']
        self._loaded = True

    def convert(self, source, target, output, diffusion_steps=25,
                length_adjust=1.0, inference_cfg_rate=0.7):
        """source: okunan metnin sesi, target: hedef (kullanici) referans sesi."""
        import torch
        import torchaudio
        import librosa
        import numpy as np

        self._ensure_loaded()

        def crossfade(chunk1, chunk2, overlap):
            fade_out = np.cos(np.linspace(0, np.pi / 2, overlap))
            fade_in = np.cos(np.linspace(np.pi / 2, 0, overlap))
            chunk2[:overlap] = chunk2[:overlap] * fade_in + chunk1[-overlap:] * fade_out
            return chunk2
        device = self.device
        sr = 22050
        hop_length = 256
        max_context_window = sr // hop_length * 30
        overlap_frame_len = 16
        overlap_wave_len = overlap_frame_len * hop_length

        source_audio = librosa.load(source, sr=sr)[0]
        ref_audio = librosa.load(target, sr=sr)[0]
        source_audio = torch.tensor(source_audio).unsqueeze(0).float().to(device)
        ref_audio = torch.tensor(ref_audio[:sr * 25]).unsqueeze(0).float().to(device)

        converted_waves_16k = torchaudio.functional.resample(source_audio, sr, 16000)
        if converted_waves_16k.size(-1) <= 16000 * 30:
            S_alt = self.semantic_fn(converted_waves_16k)
        else:
            overlapping_time = 5
            S_alt_list = []
            buffer = None
            traversed_time = 0
            while traversed_time < converted_waves_16k.size(-1):
                if buffer is None:
                    chunk = converted_waves_16k[:, traversed_time:traversed_time + 16000 * 30]
                else:
                    chunk = torch.cat(
                        [buffer, converted_waves_16k[:, traversed_time:traversed_time + 16000 * (30 - overlapping_time)]],
                        dim=-1)
                S_alt = self.semantic_fn(chunk)
                if traversed_time == 0:
                    S_alt_list.append(S_alt)
                else:
                    S_alt_list.append(S_alt[:, 50 * overlapping_time:])
                buffer = chunk[:, -16000 * overlapping_time:]
                traversed_time += 30 * 16000 if traversed_time == 0 else chunk.size(-1) - 16000 * overlapping_time
            S_alt = torch.cat(S_alt_list, dim=1)

        ori_waves_16k = torchaudio.functional.resample(ref_audio, sr, 16000)
        S_ori = self.semantic_fn(ori_waves_16k)

        mel = self.mel_fn(source_audio.to(device).float())
        mel2 = self.mel_fn(ref_audio.to(device).float())

        target_lengths = torch.LongTensor([int(mel.size(2) * length_adjust)]).to(mel.device)
        target2_lengths = torch.LongTensor([mel2.size(2)]).to(mel2.device)

        feat2 = torchaudio.compliance.kaldi.fbank(ori_waves_16k,
                                                  num_mel_bins=80, dither=0,
                                                  sample_frequency=16000)
        feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
        style2 = self.campplus_model(feat2.unsqueeze(0))

        cond, _, _, _, _ = self.model.length_regulator(
            S_alt, ylens=target_lengths, n_quantizers=3, f0=None)
        prompt_condition, _, _, _, _ = self.model.length_regulator(
            S_ori, ylens=target2_lengths, n_quantizers=3, f0=None)

        max_source_window = max_context_window - mel2.size(2)
        processed_frames = 0
        generated_wave_chunks = []
        with torch.no_grad():
            while processed_frames < cond.size(1):
                chunk_cond = cond[:, processed_frames:processed_frames + max_source_window]
                is_last_chunk = processed_frames + max_source_window >= cond.size(1)
                cat_condition = torch.cat([prompt_condition, chunk_cond], dim=1)
                vc_target = self.model.cfm.inference(
                    cat_condition,
                    torch.LongTensor([cat_condition.size(1)]).to(mel2.device),
                    mel2, style2, None, diffusion_steps,
                    inference_cfg_rate=inference_cfg_rate)
                vc_target = vc_target[:, :, mel2.size(-1):]
                vc_wave = self.vocoder_fn(vc_target.float()).squeeze()
                vc_wave = vc_wave[None, :]
                if processed_frames == 0:
                    if is_last_chunk:
                        generated_wave_chunks.append(vc_wave[0].cpu().numpy())
                        break
                    generated_wave_chunks.append(vc_wave[0, :-overlap_wave_len].cpu().numpy())
                    previous_chunk = vc_wave[0, -overlap_wave_len:]
                    processed_frames += vc_target.size(2) - overlap_frame_len
                elif is_last_chunk:
                    generated_wave_chunks.append(
                        crossfade(previous_chunk.cpu().numpy(), vc_wave[0].cpu().numpy(), overlap_wave_len))
                    processed_frames += vc_target.size(2) - overlap_frame_len
                    break
                else:
                    generated_wave_chunks.append(
                        crossfade(previous_chunk.cpu().numpy(),
                                  vc_wave[0, :-overlap_wave_len].cpu().numpy(), overlap_wave_len))
                    previous_chunk = vc_wave[0, -overlap_wave_len:]
                    processed_frames += vc_target.size(2) - overlap_frame_len
        full = torch.tensor(np.concatenate(generated_wave_chunks))[None, :].float()
        torchaudio.save(output, full.cpu(), sr)
        return output
