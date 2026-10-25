# Kebijakan Sumber Independen Kemdiktisaintek

Keputusan kebijakan: BIMA dan Pengumuman Kemdiktisaintek adalah sumber independen. Admission serta delivery Kemdiktisaintek tidak membaca atau bergantung pada referensi BIMA, hash referensi, freshness, pagination, coverage, atau hasil cross-source dedup. POLL Kemdiktisaintek hanya mengambil sumber Kemdiktisaintek.

Perlindungan internal tetap wajib: UUID stabil, baseline exclusion, activation fence, tanggal publikasi dan first-seen window yang terpercaya, detail terverifikasi, capture dan overlap sehat, klasifikasi RELEVANT, identity conflict check, admission baru yang masih berlaku, deduplikasi event dan CAS sebelum POST. HOLD/NEEDS_REVIEW historis akibat aturan BIMA lama tidak otomatis dipromosikan. Revision autosend tetap nonaktif.

State schema tetap v2. Policy envelope dimigrasikan secara eksplisit dari `kemdiktisaintek-delivery-v1` ke `kemdiktisaintek-delivery-v2-independent-sources`; migrasi meningkatkan generation, menyimpan marker dengan fingerprint state sumber, dan mempertahankan observation history, baseline, activation fence, revision log, admissions, outbox, dan receipt TEST. Tidak dilakukan reinitialize atau remote state write pada perubahan kode ini; migrasi remote berlangsung hanya saat operasi state berikutnya yang berwenang.

Kedua switch delivery tidak diubah. Setelah checkpoint kode berubah, scheduler yang masih menyetujui SHA sebelumnya harus fail closed sampai operator meninjau checkpoint baru dan memperbarui approved SHA secara terpisah.