# GPU_fan — NVIDIA GPUの温度連動ファン制御

対象GPUが載っているPC自身で実行します。監視用Docker、crontabとは独立した前景プロセスです。Python 3.9以上とNVIDIAドライバーが必要で、pipによる追加インストールは不要です。NVMLを直接利用するため、XサーバーやCoolbitsの設定変更はしません。

## 設定

`curve.json` に設定済みです。

| GPU温度 | ファン指令値 |
|---|---:|
| 50℃以下 | 40% |
| 60℃ | 60% |
| 70℃ | 80% |
| 75℃ | 90% |
| 80℃以上 | 100% |

点の間は線形補間し、端数は切り上げます。2秒間隔、増速は即時、減速は15秒待ちます。全ファンを同じ%にします。標準設定ではなく、冷却優先の調整案です。

## 手順（対象GPUのあるLinux PC上）

このリポジトリを取得して、`GPU_fan` ディレクトリへ移動します:

```bash
cd GPU_fan
python3 control.py status
```

対象GPUの名前と `GPU-...` のUUIDを確認します。ファン数、設定可能範囲も表示します。この確認は読み取りだけで、書き込みが許可されることまで保証するものではありません。`Not Supported` の場合はその機種・ドライバーでの対応確認が必要です。

共有パスが見えない場合は `GPU_fan` フォルダー全体を対象PCのローカル領域へコピーし、以降のパスを読み替えてください。

以下の `GPU-ここにUUID` をstatusの表示値へ置き換えてください。インデックス番号ではなくUUIDを指定して、意図しない別GPUの変更を防ぎます。

```bash
# 変更せず30秒間、温度に対する設定予定値を見る
python3 control.py run --uuid GPU-ここにUUID --dry-run --duration 30

# まず10分間だけ実際に制御。10分後は標準の自動制御へ戻る
sudo python3 control.py run --uuid GPU-ここにUUID --duration 600

# 継続運転（Ctrl+Cで停止し、自動制御へ戻る）
sudo python3 control.py run --uuid GPU-ここにUUID
```

別ターミナルで確認:

```bash
watch -n 2 nvidia-smi --query-gpu=name,uuid,temperature.gpu,fan.speed --format=csv
```

手動で標準の自動制御に戻す場合は、先に実行中のコントローラーをCtrl+Cで止めてから:

```bash
sudo python3 control.py auto --uuid GPU-ここにUUID
```

## 動作と制約

- GPU設定は `run`（dry-runなし）または `auto` をユーザーが実行した場合だけ変更します。
- そのGPUの全ファンを制御し、他のGPUや消費電力上限は変更しません。
- 同じGPUでコントローラーが重複起動しないようロックします。ロックはLinuxの `/run/lock` に置きます。
- 設定カーブが実機の設定可能範囲に収まらなければ、変更前に停止します。
- 取得APIが使えても、書き込みが `Not Supported` / `No Permission` になる場合があります。その際は失敗を表示して終了します。VBIOSやドライバーの制限を回避する機能はありません。
- Ctrl+C、SIGTERM、SIGHUP、制御中の例外や温度取得失敗時には、変更したGPUの全ファンへ標準の自動制御への復帰を試みます。復帰に失敗した場合もエラーを表示します。
- 強制終了（`kill -9`）、OS停止、ドライバー異常では終了処理を実行できません。再接続後に上記 `auto` で戻してください。初回は前景で温度とファン指令値を確認してください。
- これは常駐プロセスなので、毎分起動するcrontabには追加しません。このプロセスの実行中だけカーブを適用します。SSH切断（SIGHUP）でも自動制御へ戻す設計です。

API仕様: [NVIDIA NVML Device Commands](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceCommands.html)、[Device Queries](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html)。手動ファン制御は標準の温度制御を置き換えるため、コントローラーの実行と終了処理が必要です。

## 検証

```bash
python3 -m unittest discover -s tests -p test_fan.py -v
```

補間、減速待ち、ドライランの非変更、複数ファンへの適用、部分失敗・温度取得失敗時の自動復帰を、GPU設定を変更しないモックで検証します。

## 管理者権限で常駐・再起動後も自動起動（systemd）

対象GPUのPC上で実行します。まず前景で動かしている制御をCtrl+Cで停止してください。

```bash
cd GPU_fan
python3 control.py status
# 対象GPUのUUIDへ置き換える
GPU_UUID='GPU-ここにUUID'
sudo bash install-service.sh "$GPU_UUID"
sudo systemctl enable --now "gpu-fan-curve@$GPU_UUID.service"
sudo systemctl status "gpu-fan-curve@$GPU_UUID.service" --no-pager
```

インストーラーはコードを `/opt/gpu-fan-control/<UUID>/control.py`、設定を `/etc/gpu-fan-control/<UUID>.json` へroot所有でコピーします。実行時は共有フォルダーやユーザーのホームに依存しません。インストーラー単独ではファン設定を変えず、`enable --now` で初めてサービスを起動します。サービスはroot権限で実行し、ログアウト後も継続、OS起動時に自動起動します。異常終了は10秒後に再試行します。設定変更後はサービスの再起動が必要です。

ログと温度確認:

```bash
sudo journalctl -u "gpu-fan-curve@$GPU_UUID.service" -n 30 --no-pager
sudo journalctl -u "gpu-fan-curve@$GPU_UUID.service" -f
# 別ターミナル
watch -n 2 nvidia-smi --query-gpu=name,uuid,temperature.gpu,fan.speed --format=csv
```

停止し、自動起動も解除する場合:

```bash
sudo systemctl disable --now "gpu-fan-curve@$GPU_UUID.service"
```

停止時はSIGTERMによる終了処理と `ExecStopPost` で標準の自動制御への復帰を試みます。GPUやドライバー自体が応答不能な場合は復帰できないため、ログの確認が必要です。`Not Supported` などが繰り返される場合は上記の停止コマンドで止めてください。常駐版と前景版を同じGPUへ同時に起動しないでください。

カーブ変更:

```bash
sudo nano "/etc/gpu-fan-control/$GPU_UUID.json"
sudo systemctl restart "gpu-fan-curve@$GPU_UUID.service"
```

サービスの動作は [systemd.service公式仕様](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) に基づきます。

導入済みサービスは `/opt` と `/etc` にコピーしたファイルを使うため、取得したソースフォルダーを移動しても再導入する必要はありません。
